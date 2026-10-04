import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local import orchestrator
from local.config import load_config
from tests.test_orchestrator_resume import certify_shard, create_shard_db


def result_payload(region_hash: str, *, status: str = "complete") -> dict:
    target_status = "complete" if status == "complete" else "failed"
    return {
        "schema_version": 1,
        "status": status,
        "mode": "full",
        "region_json_sha256": region_hash,
        "targets": [
            {
                "name": "Root Leaf",
                "url": "https://example.test/leaf",
                "status": target_status,
                **({"error": "boom"} if target_status == "failed" else {}),
            }
        ],
        "expected_target_count": 1,
        "failed_target_count": 0 if status == "complete" else 1,
        "saved": status == "complete",
    }


def shard_config(root: Path, retry: int = 0):
    return SimpleNamespace(
        project_root=root,
        python_executable="python3",
        region_json_path=root / "source-regions.json",
        crawl=SimpleNamespace(
            shard_total=1,
            shard_groups=["0-0"],
            max_local_parallel=1,
            retry_failed_shards=retry,
            shard_timeout_minutes=1,
            network_ready_retry_attempts=1,
            network_ready_retry_delay_seconds=0,
            max_concurrent_pages=1,
            max_api_prefetch_concurrency=1,
            navigation_retry_attempts=1,
            max_failed_region_ratio=0,
            max_rate_limit_responses=1,
            article_response_timeout_ms=100,
            max_article_pages_per_trade=1,
            max_article_missing_complex_ratio=0,
            min_complexes_for_article_check=1,
        ),
    )


def create_empty_shard_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3

    with sqlite3.connect(path) as conn:
        conn.execute("create table complexes (complex_no integer primary key)")
        conn.execute(
            "create table prices (id integer primary key, complex_no integer, date text)"
        )


class CompletionCertificateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.shards = self.root / "shards"
        self.logs = self.root / "logs"
        self.snapshot = self.root / "snapshot.json"
        self.snapshot.write_text(
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
        self.snapshot_hash = orchestrator.sha256_file(self.snapshot)
        self.paths = {
            "shards": self.shards,
            "logs": self.logs,
            "screenshots": self.root / "screenshots",
            "region_json": self.snapshot,
            "region_json_sha256": self.snapshot_hash,
        }
        self.config = shard_config(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_zero_exit_valid_result_creates_db_bound_atomic_proof(self):
        shard_db = self.shards / "db_shard_0.db"

        def fake_command(*_args, **_kwargs):
            create_empty_shard_db(shard_db)
            orchestrator.write_json(
                orchestrator.shard_result_path(shard_db),
                result_payload(self.snapshot_hash),
            )
            return SimpleNamespace(returncode=0)

        with patch("local.orchestrator.wait_for_network_ready", return_value=True), patch(
            "local.orchestrator.run_command", side_effect=fake_command
        ):
            ok, payload = orchestrator.run_shard(self.config, self.paths, 0)

        self.assertTrue(ok)
        proof_path = orchestrator.shard_completion_path(shard_db)
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        self.assertEqual(proof["db_sha256"], orchestrator.sha256_file(shard_db))
        self.assertEqual(proof["result_sha256"], orchestrator.sha256_file(orchestrator.shard_result_path(shard_db)))
        self.assertEqual(proof["region_json_sha256"], self.snapshot_hash)
        self.assertEqual((proof["shard_index"], proof["shard_total"]), (0, 1))
        self.assertEqual(payload["price_count"], 0)
        self.assertEqual(payload["complex_count"], 0)

    def test_complete_empty_target_set_is_certified_when_expected_is_positive(self):
        shard_db = self.shards / "db_shard_0.db"
        payload = result_payload(self.snapshot_hash)
        payload["targets"][0]["status"] = "empty"
        create_empty_shard_db(shard_db)
        result_path = orchestrator.shard_result_path(shard_db)
        orchestrator.write_json(result_path, payload)

        proof = orchestrator.certify_shard_completion(
            shard_db,
            result_path,
            self.snapshot,
            self.snapshot_hash,
            0,
            1,
        )

        self.assertEqual(proof["expected_target_count"], 1)
        self.assertEqual(proof["failed_target_count"], 0)
        self.assertIsNotNone(
            orchestrator.inspect_certified_shard(
                shard_db, self.snapshot, self.snapshot_hash, 0, 1
            )
        )

    def test_zero_exit_failed_result_never_certifies_shard(self):
        shard_db = self.shards / "db_shard_0.db"

        def fake_command(*_args, **_kwargs):
            create_shard_db(shard_db)
            orchestrator.write_json(
                orchestrator.shard_result_path(shard_db),
                result_payload(self.snapshot_hash, status="failed"),
            )
            return SimpleNamespace(returncode=0)

        with patch("local.orchestrator.wait_for_network_ready", return_value=True), patch(
            "local.orchestrator.run_command", side_effect=fake_command
        ):
            ok, _payload = orchestrator.run_shard(self.config, self.paths, 0)

        self.assertFalse(ok)
        self.assertFalse(orchestrator.shard_completion_path(shard_db).exists())
        self.assertFalse(shard_db.exists())
        self.assertTrue((self.shards / "archive").is_dir())

    def test_resume_rejects_tampered_db_even_with_existing_proof(self):
        shard_db = self.shards / "db_shard_0.db"
        create_shard_db(shard_db)
        certify_shard(shard_db, self.snapshot, self.snapshot_hash, 0, 1)
        with shard_db.open("ab") as stream:
            stream.write(b"tamper")

        self.assertIsNone(
            orchestrator.inspect_certified_shard(
                shard_db, self.snapshot, self.snapshot_hash, 0, 1
            )
        )

    def test_crash_after_result_before_proof_is_not_resumable(self):
        shard_db = self.shards / "db_shard_0.db"
        create_shard_db(shard_db)
        orchestrator.write_json(
            orchestrator.shard_result_path(shard_db),
            result_payload(self.snapshot_hash),
        )

        self.assertIsNone(
            orchestrator.inspect_certified_shard(
                shard_db, self.snapshot, self.snapshot_hash, 0, 1
            )
        )

    def test_result_rejects_duplicate_urls_and_count_mismatch(self):
        payload = result_payload(self.snapshot_hash)
        payload["targets"].append(dict(payload["targets"][0]))
        payload["expected_target_count"] = 2
        with self.assertRaises(orchestrator.PipelineError):
            orchestrator.validate_crawler_result(
                payload,
                self.snapshot_hash,
                [{"name": "Root Leaf", "url": "https://example.test/leaf"}],
            )

    def test_result_requires_full_mode_saved_commit_and_exact_shard_targets(self):
        expected = [{"name": "Root Leaf", "url": "https://example.test/leaf"}]
        for mutation in (
            {"mode": "backfill"},
            {"saved": False},
            {
                "targets": [
                    {
                        "name": "Wrong Leaf",
                        "url": "https://example.test/leaf",
                        "status": "complete",
                    }
                ]
            },
        ):
            payload = result_payload(self.snapshot_hash)
            payload.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(
                orchestrator.PipelineError
            ):
                orchestrator.validate_crawler_result(
                    payload, self.snapshot_hash, expected
                )


class ConfigIntegrityTests(unittest.TestCase):
    def write_config(self, crawl: dict) -> Path:
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps({"crawl": crawl}), encoding="utf-8")
        return path

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def test_rejects_nonpositive_concurrency_and_total(self):
        for crawl in (
            {"shard_total": 0, "shard_groups": []},
            {"shard_total": 1, "shard_groups": ["0-0"], "max_local_parallel": 0},
            {"shard_total": 1, "shard_groups": ["0-0"], "max_concurrent_pages": -1},
            {"shard_total": 1, "shard_groups": ["0-0"], "max_api_prefetch_concurrency": 0},
        ):
            with self.subTest(crawl=crawl), self.assertRaises(ValueError):
                load_config(self.write_config(crawl))

    def test_rejects_negative_duplicate_and_missing_shard_coverage(self):
        invalid_groups = [
            ["-1-0", "1-2"],
            ["0-1", "1-2"],
            ["0-0", "2-2"],
        ]
        for groups in invalid_groups:
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                load_config(
                    self.write_config({"shard_total": 3, "shard_groups": groups})
                )


class PipelineFailClosedTests(unittest.TestCase):
    def test_run_exports_rejects_failed_or_missing_configured_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = {
                "exports": root / "exports",
                "logs": root / "logs",
                "merged_db": root / "merged.db",
            }
            paths["exports"].mkdir()
            paths["logs"].mkdir()
            config = SimpleNamespace(
                project_root=root,
                python_executable="python3",
                export_jobs=[
                    SimpleNamespace(
                        name="configured",
                        min_households=100,
                        regions=["서울"],
                    )
                ],
            )
            output = paths["exports"] / "export_2026-09-22_configured.xlsx"

            output.write_bytes(b"stale")
            with patch(
                "local.orchestrator.run_command",
                return_value=SimpleNamespace(returncode=2),
            ), self.assertRaises(orchestrator.PipelineError):
                orchestrator.run_exports(config, paths, date_str="2026-09-22")

            output.unlink()
            with patch(
                "local.orchestrator.run_command",
                return_value=SimpleNamespace(returncode=0),
            ), self.assertRaises(orchestrator.PipelineError):
                orchestrator.run_exports(config, paths, date_str="2026-09-22")

    def test_lock_contention_does_not_create_or_mutate_run_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(runtime_root=root / "runtime")
            lock_path = config.runtime_root / "locks" / "daily_crawl.lock"

            with orchestrator.file_lock(lock_path, "active", "test"):
                code = orchestrator.run_pipeline(config, "blocked", resume=False)

            self.assertEqual(code, 0)
            self.assertFalse((config.runtime_root / "runs" / "blocked").exists())

    def test_new_run_rejects_existing_directory_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(runtime_root=root / "runtime")
            run_dir = config.runtime_root / "runs" / "existing"
            run_dir.mkdir(parents=True)
            marker = run_dir / "marker.txt"
            marker.write_text("preserve", encoding="utf-8")

            with patch(
                "local.orchestrator.preflight"
            ) as preflight, self.assertRaises(orchestrator.PipelineError):
                orchestrator.run_pipeline(config, "existing", resume=False)

            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
            self.assertEqual(list(run_dir.iterdir()), [marker])
            preflight.assert_not_called()

    def test_initial_summary_write_occurs_while_shared_lock_is_held(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = shard_config(root)
            config.region_json_path.write_text("{}", encoding="utf-8")
            config = SimpleNamespace(
                **config.__dict__,
                runtime_root=root / "runtime",
                preflight_smoke_test=False,
                google_drive=SimpleNamespace(enabled=False),
                discord=SimpleNamespace(enabled=False),
                retention_days=14,
            )
            lock_path = config.runtime_root / "locks" / "daily_crawl.lock"
            original_write_json = orchestrator.write_json
            lock_states = []

            def checked_write(path, payload):
                lock_states.append(lock_path.exists())
                original_write_json(path, payload)

            with patch(
                "local.orchestrator.write_json", side_effect=checked_write
            ), patch(
                "local.orchestrator.preflight",
                side_effect=orchestrator.PipelineError("stop after initialization"),
            ):
                code = orchestrator.run_pipeline(config, "run", resume=False)

            self.assertEqual(code, 1)
            self.assertTrue(lock_states)
            self.assertTrue(all(lock_states))

    def test_failed_shard_stops_before_merge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = shard_config(root)
            config.region_json_path.write_text("{}", encoding="utf-8")
            config = SimpleNamespace(
                **config.__dict__,
                runtime_root=root / "runtime",
                preflight_smoke_test=False,
                google_drive=SimpleNamespace(enabled=False),
                discord=SimpleNamespace(enabled=False),
                retention_days=14,
            )

            def fail_shards(_config, paths, summary, resume=False):
                summary["completed_shards"] = []
                summary["failed_shards"] = [0]
                summary["shard_results"] = {"0": {"shard": 0, "status": "failed"}}
                orchestrator.write_json(paths["summary"], summary)

            with patch("local.orchestrator.preflight"), patch(
                "local.orchestrator.run_all_shards", side_effect=fail_shards
            ) as run_all, patch("local.orchestrator.run_merge") as merge:
                code = orchestrator.run_pipeline(config, "run", resume=False)

            self.assertEqual(code, 1)
            run_all.assert_called_once()
            merge.assert_not_called()
            summary = json.loads(
                (root / "runtime/runs/run/summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "failure")
            self.assertFalse((root / "runtime/latest").exists())

    def test_upload_failure_is_resumable_nonzero_and_not_latest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = shard_config(root)
            config.region_json_path.write_text("{}", encoding="utf-8")
            config = SimpleNamespace(
                **config.__dict__,
                runtime_root=root / "runtime",
                preflight_smoke_test=False,
                export_jobs=[],
                google_drive=SimpleNamespace(
                    enabled=True, folder_id="folder", token_file=root / "token.json"
                ),
                discord=SimpleNamespace(enabled=False),
                retention_days=14,
            )

            def complete_shards(_config, paths, summary, resume=False):
                summary["completed_shards"] = [0]
                summary["failed_shards"] = []
                summary["shard_results"] = {
                    "0": {"shard": 0, "status": "completed"}
                }
                orchestrator.write_json(paths["summary"], summary)

            def make_merge(_config, paths):
                paths["merged_db"].write_bytes(b"merged")
                return paths["merged_db"]

            with patch("local.orchestrator.preflight"), patch(
                "local.orchestrator.run_all_shards", side_effect=complete_shards
            ), patch("local.orchestrator.run_merge", side_effect=make_merge), patch(
                "local.orchestrator.validate_merged_db_quality", return_value={}
            ), patch("local.orchestrator.run_exports", return_value=[]), patch(
                "local.orchestrator.upload_files",
                return_value=[{"file": "merged.db", "success": False}],
            ):
                code = orchestrator.run_pipeline(config, "run", resume=False)

            summary_path = root / "runtime/runs/run/summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(code, 1)
            self.assertEqual(summary["status"], "partial_success")
            self.assertEqual(summary["stage"], "delivery_failed")
            self.assertFalse((root / "runtime/latest").exists())
            self.assertEqual(
                orchestrator.find_latest_resumable_run(config.runtime_root, 1), "run"
            )

    def test_export_failure_stops_before_upload_and_latest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = shard_config(root)
            config.region_json_path.write_text("{}", encoding="utf-8")
            config = SimpleNamespace(
                **config.__dict__,
                runtime_root=root / "runtime",
                preflight_smoke_test=False,
                export_jobs=[SimpleNamespace(name="required")],
                google_drive=SimpleNamespace(enabled=False),
                discord=SimpleNamespace(enabled=False),
                retention_days=14,
            )

            def complete_shards(_config, paths, summary, resume=False):
                summary["completed_shards"] = [0]
                summary["failed_shards"] = []
                summary["shard_results"] = {
                    "0": {"shard": 0, "status": "completed"}
                }
                orchestrator.write_json(paths["summary"], summary)

            def make_merge(_config, paths):
                paths["merged_db"].write_bytes(b"merged")
                return paths["merged_db"]

            with patch("local.orchestrator.preflight"), patch(
                "local.orchestrator.run_all_shards", side_effect=complete_shards
            ), patch(
                "local.orchestrator.run_merge", side_effect=make_merge
            ), patch(
                "local.orchestrator.validate_merged_db_quality", return_value={}
            ), patch(
                "local.orchestrator.run_exports",
                side_effect=orchestrator.PipelineError("required export missing"),
            ), patch("local.orchestrator.upload_files") as upload:
                code = orchestrator.run_pipeline(config, "run", resume=False)

            summary = json.loads(
                (root / "runtime/runs/run/summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(code, 1)
            self.assertEqual(summary["status"], "failure")
            self.assertIn("required export missing", summary["error_stage"])
            upload.assert_not_called()
            self.assertFalse((root / "runtime/latest").exists())


if __name__ == "__main__":
    unittest.main()
