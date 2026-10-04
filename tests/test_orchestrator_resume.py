import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local import orchestrator


def create_shard_db(path: Path, price_count: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("create table complexes (complex_no integer primary key)")
        conn.execute(
            """
            create table prices (
                id integer primary key autoincrement,
                complex_no integer,
                date text
            )
            """
        )
        conn.execute("insert into complexes values (1)")
        for _ in range(price_count):
            conn.execute(
                "insert into prices (complex_no, date) values (1, '2026-08-25')"
            )


def certify_shard(
    shard_db: Path,
    snapshot: Path,
    snapshot_hash: str,
    shard_index: int,
    shard_total: int,
) -> None:
    expected_targets = orchestrator.expected_shard_targets(
        snapshot, shard_index, shard_total
    )
    result_path = orchestrator.shard_result_path(shard_db)
    orchestrator.write_json(
        result_path,
        {
            "schema_version": 1,
            "status": "complete",
            "mode": "full",
            "region_json_sha256": snapshot_hash,
            "targets": [
                {
                    **target,
                    "status": "complete",
                }
                for target in expected_targets
            ],
            "expected_target_count": len(expected_targets),
            "failed_target_count": 0,
            "saved": True,
        },
    )
    orchestrator.certify_shard_completion(
        shard_db,
        result_path,
        snapshot,
        snapshot_hash,
        shard_index,
        shard_total,
    )


class ResumeShardTests(unittest.TestCase):
    def test_inspect_shard_db_checks_structure_not_price_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid = root / "valid.db"
            empty = root / "empty.db"
            broken = root / "broken.db"
            create_shard_db(valid, price_count=3)
            create_shard_db(empty, price_count=0)
            broken.write_text("not sqlite", encoding="utf-8")

            self.assertEqual(orchestrator.inspect_shard_db(valid)["price_count"], 3)
            self.assertEqual(orchestrator.inspect_shard_db(empty)["price_count"], 0)
            self.assertIsNone(orchestrator.inspect_shard_db(broken))
            self.assertIsNone(orchestrator.inspect_shard_db(root / "missing.db"))

    def test_resume_skips_valid_shards_and_runs_only_missing_shards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = {
                "shards": root / "shards",
                "logs": root / "logs",
                "summary": root / "summary.json",
            }
            snapshot = root / "regions.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "Root": {
                            "children": {
                                f"Leaf {index}": {
                                    "url": f"https://example.test/{index}",
                                    "has_complexes": True,
                                }
                                for index in range(4)
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            snapshot_hash = orchestrator.sha256_file(snapshot)
            for shard_index, price_count in ((0, 5), (1, 7)):
                shard_db = paths["shards"] / f"db_shard_{shard_index}.db"
                create_shard_db(shard_db, price_count=price_count)
                certify_shard(shard_db, snapshot, snapshot_hash, shard_index, 4)
            paths["region_json"] = snapshot
            paths["region_json_sha256"] = snapshot_hash

            config = SimpleNamespace(
                crawl=SimpleNamespace(
                    shard_total=4,
                    shard_groups=["0-1", "2-3"],
                    max_local_parallel=1,
                )
            )
            summary = {
                "completed_shards": [],
                "failed_shards": [],
                "groups": [],
                "shard_results": {},
            }
            executed = []

            def fake_run_shard(_config, _paths, shard_index):
                executed.append(shard_index)
                create_shard_db(
                    _paths["shards"] / f"db_shard_{shard_index}.db",
                    price_count=10 + shard_index,
                )
                return True, {
                    "shard": shard_index,
                    "attempts": 1,
                    "db_path": str(
                        _paths["shards"] / f"db_shard_{shard_index}.db"
                    ),
                    "log_path": str(_paths["logs"] / f"shard_{shard_index}.log"),
                    "price_count": 10 + shard_index,
                }

            with patch("local.orchestrator.run_shard", side_effect=fake_run_shard):
                orchestrator.run_all_shards(config, paths, summary, resume=True)

            self.assertEqual(executed, [2, 3])
            self.assertEqual(summary["completed_shards"], [0, 1, 2, 3])
            self.assertEqual(summary["failed_shards"], [])
            self.assertTrue(summary["shard_results"]["0"]["reused"])
            self.assertTrue(summary["shard_results"]["1"]["reused"])
            persisted = json.loads(paths["summary"].read_text(encoding="utf-8"))
            self.assertEqual(persisted["completed_shards"], [0, 1, 2, 3])

    def test_interrupted_incomplete_shard_is_rerun(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = {
                "shards": root / "shards",
                "logs": root / "logs",
                "summary": root / "summary.json",
            }
            snapshot = root / "regions.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "Root": {
                            "children": {
                                "Leaf": {
                                    "url": "https://example.test/leaf",
                                    "has_complexes": True,
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            snapshot_hash = orchestrator.sha256_file(snapshot)
            shard_db = paths["shards"] / "db_shard_0.db"
            create_shard_db(shard_db, price_count=3)
            orchestrator.write_json(
                orchestrator.shard_result_path(shard_db),
                {"status": "failed"},
            )
            orchestrator.write_json(
                orchestrator.shard_completion_path(shard_db),
                {"status": "complete", "db_sha256": "stale"},
            )
            paths["region_json"] = snapshot
            paths["region_json_sha256"] = snapshot_hash
            config = SimpleNamespace(
                crawl=SimpleNamespace(
                    shard_total=1,
                    shard_groups=["0-0"],
                    max_local_parallel=1,
                )
            )
            summary = {
                "completed_shards": [],
                "failed_shards": [],
                "groups": [],
                "shard_results": {},
            }

            def fake_run_shard(_config, _paths, shard_index):
                return True, {
                    "shard": shard_index,
                    "attempts": 1,
                    "db_path": str(_paths["shards"] / "db_shard_0.db"),
                    "log_path": str(_paths["logs"] / "shard_0.log"),
                    "price_count": 9,
                }

            with patch(
                "local.orchestrator.run_shard", side_effect=fake_run_shard
            ) as run_shard:
                orchestrator.run_all_shards(config, paths, summary, resume=True)

            run_shard.assert_called_once()
            archive_files = {
                item.name
                for item in (paths["shards"] / "archive").rglob("*")
                if item.is_file()
            }
            self.assertEqual(
                archive_files,
                {
                    "db_shard_0.db",
                    "db_shard_0.db.result.json",
                    "db_shard_0.db.complete.json",
                },
            )


class RunInputSnapshotTests(unittest.TestCase):
    def test_resume_keeps_original_region_snapshot_when_source_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "regions.json"
            source.write_text('{"version": 1}', encoding="utf-8")
            paths = {
                "region_snapshot": root / "run" / "inputs" / "regions.json"
            }
            paths["region_snapshot"].parent.mkdir(parents=True)
            config = SimpleNamespace(
                region_json_path=source,
                crawl=SimpleNamespace(
                    shard_total=20,
                    shard_groups=["0-19"],
                ),
            )
            summary = {}

            orchestrator.prepare_run_inputs(config, paths, summary, resume=False)
            original_hash = summary["region_json_sha256"]
            source.write_text('{"version": 2}', encoding="utf-8")
            orchestrator.prepare_run_inputs(config, paths, summary, resume=True)

            self.assertEqual(summary["region_json_sha256"], original_hash)
            self.assertEqual(
                paths["region_snapshot"].read_text(encoding="utf-8"),
                '{"version": 1}',
            )

    def test_resume_rejects_changed_shard_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "regions.json"
            source.write_text("{}", encoding="utf-8")
            snapshot = root / "run" / "inputs" / "regions.json"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text("{}", encoding="utf-8")
            config = SimpleNamespace(
                region_json_path=source,
                crawl=SimpleNamespace(shard_total=40, shard_groups=["0-39"]),
            )
            summary = {
                "region_json_sha256": orchestrator.sha256_file(snapshot),
                "region_json_snapshot": str(snapshot),
                "shard_total": 20,
                "configured_shard_groups": ["0-19"],
            }

            with self.assertRaises(orchestrator.PipelineError):
                orchestrator.prepare_run_inputs(
                    config,
                    {"region_snapshot": snapshot},
                    summary,
                    resume=True,
                )

    def test_resume_rejects_legacy_run_without_snapshot_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "regions.json"
            source.write_text("{}", encoding="utf-8")
            config = SimpleNamespace(
                region_json_path=source,
                crawl=SimpleNamespace(shard_total=1, shard_groups=["0-0"]),
            )
            with self.assertRaisesRegex(
                orchestrator.PipelineError, "legacy|metadata|snapshot"
            ):
                orchestrator.prepare_run_inputs(
                    config,
                    {"region_snapshot": root / "missing-snapshot.json"},
                    {},
                    resume=True,
                )


class RecoverySelectionTests(unittest.TestCase):
    def test_newer_success_prevents_recovery_of_stale_older_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runs = Path(temp_dir) / "runs"
            runs.mkdir()

            payloads = {
                "20260823_010000_kst": {
                    "status": "running",
                    "completed_shards": [0, 1],
                    "failed_shards": [],
                },
                "20260824_010000_kst": {
                    "status": "success",
                    "completed_shards": list(range(20)),
                    "failed_shards": [],
                },
                "20260825_010000_kst": {
                    "status": "failure",
                    "completed_shards": list(range(20)),
                    "failed_shards": [],
                },
            }
            for run_id, payload in payloads.items():
                run_dir = runs / run_id
                run_dir.mkdir()
                (run_dir / "summary.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )

            self.assertIsNone(orchestrator.find_latest_resumable_run(runs.parent, 20))

    def test_skipped_lock_run_does_not_hide_active_older_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runs = Path(temp_dir) / "runs"
            payloads = {
                "20260824_010000_kst": {
                    "status": "running",
                    "completed_shards": [0, 1],
                    "failed_shards": [],
                },
                "20260825_010000_kst": {
                    "status": "skipped_due_to_lock",
                    "completed_shards": [],
                    "failed_shards": [],
                },
            }
            for run_id, payload in payloads.items():
                run_dir = runs / run_id
                run_dir.mkdir(parents=True)
                (run_dir / "summary.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )

            self.assertEqual(
                orchestrator.find_latest_resumable_run(runs.parent, 20),
                "20260824_010000_kst",
            )

    def test_latest_incomplete_failure_is_resumable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runs = Path(temp_dir) / "runs"
            run_dir = runs / "20260825_010000_kst"
            run_dir.mkdir(parents=True)
            (run_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "status": "failure",
                        "completed_shards": list(range(14)),
                        "failed_shards": list(range(14, 20)),
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                orchestrator.find_latest_resumable_run(runs.parent, 20),
                "20260825_010000_kst",
            )

    def test_upload_partial_success_is_resumable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runs = Path(temp_dir) / "runs"
            run_dir = runs / "20260921_010000_kst"
            run_dir.mkdir(parents=True)
            (run_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "status": "partial_success",
                        "stage": "delivery_failed",
                        "completed_shards": list(range(20)),
                        "failed_shards": [],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                orchestrator.find_latest_resumable_run(runs.parent, 20),
                "20260921_010000_kst",
            )


if __name__ == "__main__":
    unittest.main()
