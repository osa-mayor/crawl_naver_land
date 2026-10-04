import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local import backfill
from local.lock import LockMetadata
from merge_db import init_master_db


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def create_db(path: Path, complexes=(), prices=()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        init_master_db(connection.cursor())
        connection.executemany(
            "insert into complexes (complex_no, name, total_households) values (?, ?, ?)",
            complexes,
        )
        connection.executemany(
            """
            insert into prices (
                complex_no, date, pyeong_type, supply_area, exclusive_area,
                trade_avg, trade_count
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            prices,
        )


def valid_result(targets, region_hash):
    return {
        "schema_version": 1,
        "status": "complete",
        "mode": "backfill",
        "saved": True,
        "region_json_sha256": region_hash,
        "expected_target_count": len(targets),
        "failed_target_count": 0,
        "targets": [
            {
                **target,
                "status": "complete",
                "total": 2,
                "ids": ["1", "2"],
                "apartment_ids": ["1", "2"],
            }
            for target in targets
        ],
    }


class BackfillUnitTests(unittest.TestCase):
    def test_run_wide_manifest_rejects_changed_resume_and_mid_run_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, region, code = [root / name for name in ('source', 'region', 'code')]
            for path in (source, region, code):
                path.write_text('original')
            pinned = {
                'source_db_sha256': backfill.sha256_file(source),
                'region_json_sha256': backfill.sha256_file(region),
                'code_sha256': {'crawler': backfill.sha256_file(code)},
            }
            manifest = root / 'manifest.json'
            backfill.pin_run_inputs(manifest, pinned)
            backfill.verify_run_inputs(pinned, source, region, {'crawler': code})
            code.write_text('changed')
            with self.assertRaises(backfill.BackfillError):
                backfill.verify_run_inputs(pinned, source, region, {'crawler': code})
            with self.assertRaises(backfill.BackfillError):
                backfill.pin_run_inputs(manifest, {**pinned, 'source_db_sha256': 'changed'})
            self.assertEqual(backfill.read_json(manifest), pinned)

    def test_leaf_walk_ignores_false_parent_but_excludes_false_leaf_and_root(self):
        payload = {
            "RootOnly": {"url": "root", "has_complexes": True},
            "Province": {
                "has_complexes": False,
                "children": {
                    "District": {
                        "children": {
                            "Good": {"url": "good", "has_complexes": True},
                            "Empty": {"url": "empty", "has_complexes": False},
                        }
                    }
                },
            },
        }
        self.assertEqual(
            backfill.valid_leaf_targets(payload),
            [{"name": "Province District Good", "url": "good"}],
        )

    def test_result_validation_requires_saved_backfill_and_consistent_unique_subset(self):
        targets = [{"name": "Songdo", "url": "https://example.test/songdo"}]
        result = valid_result(targets, "abc")
        self.assertIs(backfill.validate_crawler_result(result, targets, "abc"), result)

        for field, value in (("mode", "full"), ("saved", False)):
            invalid = dict(result)
            invalid[field] = value
            with self.assertRaises(backfill.BackfillError):
                backfill.validate_crawler_result(invalid, targets, "abc")

        invalid = json.loads(json.dumps(result))
        invalid["targets"][0]["ids"] = ["1", "1"]
        with self.assertRaises(backfill.BackfillError):
            backfill.validate_crawler_result(invalid, targets, "abc")

        invalid = json.loads(json.dumps(result))
        invalid["targets"][0]["apartment_ids"] = ["3"]
        with self.assertRaises(backfill.BackfillError):
            backfill.validate_crawler_result(invalid, targets, "abc")

        invalid = dict(result)
        invalid["expected_target_count"] = True
        with self.assertRaises(backfill.BackfillError):
            backfill.validate_crawler_result(invalid, targets, "abc")

    def test_certification_is_bound_to_all_inputs_and_db(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.db"
            batch = root / "batch.db"
            region = root / "region.json"
            target_path = root / "targets.json"
            code = root / "crawler.py"
            result_path = Path(str(batch) + ".result.json")
            proof_path = Path(str(batch) + ".complete.json")
            targets = [{"name": "Songdo", "url": "songdo"}]
            create_db(source)
            create_db(batch)
            write_json(region, {"region": True})
            write_json(target_path, targets)
            code.write_text("first", encoding="utf-8")
            binding = backfill.binding_for_batch(
                batch_index=0,
                target_path=target_path,
                source_db=source,
                region_snapshot=region,
                code_paths={"crawler": code},
            )
            write_json(result_path, valid_result(targets, binding["region_json_sha256"]))
            backfill.certify_batch(batch, result_path, proof_path, targets, binding)
            self.assertIsNotNone(
                backfill.inspect_certified_batch(
                    batch, result_path, proof_path, targets, binding
                )
            )

            code.write_text("changed", encoding="utf-8")
            changed = backfill.binding_for_batch(
                batch_index=0,
                target_path=target_path,
                source_db=source,
                region_snapshot=region,
                code_paths={"crawler": code},
            )
            self.assertIsNone(
                backfill.inspect_certified_batch(
                    batch, result_path, proof_path, targets, changed
                )
            )

    def test_merge_uses_copy_and_never_mutates_or_replaces_source_prices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.db"
            batch = root / "batch.db"
            output = root / "merged.db"
            identity = (1, "2026-09-22", "84A", 112.0, 84.0)
            create_db(
                source,
                complexes=[(1, "original", 100)],
                prices=[(*identity, 100, 1)],
            )
            create_db(
                batch,
                complexes=[(1, "new metadata", 100), (2, "metadata-only retried", 200)],
                prices=[(*identity, 999, 9), (2, "2026-09-22", "59", 80.0, 59.0, 200, 2)],
            )
            source_hash = backfill.sha256_file(source)
            metrics = backfill.merge_from_copy(source, [batch], output)
            self.assertEqual(backfill.sha256_file(source), source_hash)
            self.assertEqual(metrics["removed_source_price_conflicts"], 1)
            with sqlite3.connect(output) as connection:
                self.assertEqual(
                    connection.execute(
                        "select trade_avg, trade_count from prices where complex_no=1"
                    ).fetchone(),
                    (100, 1),
                )
                self.assertEqual(
                    connection.execute(
                        "select trade_avg from prices where complex_no=2"
                    ).fetchone(),
                    (200,),
                )


class BackfillDriverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.source_run = self.runtime / "runs" / "source_run"
        self.source_db = self.source_run / "merged.db"
        self.region = self.source_run / "inputs" / "naver_region_codes.json"
        create_db(
            self.source_db,
            complexes=[(1, "priced", 100), (2, "metadata-only", 200)],
            prices=[(1, "2026-09-21", "84", 112.0, 84.0, 100, 1)],
        )
        write_json(self.source_run / "summary.json", {"status": "success"})
        write_json(
            self.region,
            {
                "Incheon": {
                    "children": {
                        "Yeonsu": {
                            "children": {
                                "Songdo": {
                                    "url": "https://example.test/songdo",
                                    "has_complexes": True,
                                }
                            }
                        }
                    }
                }
            },
        )
        for filename in ("crawler.py", "land_selectors.py", "merge_db.py", "export_db.py"):
            (self.root / filename).write_text(f"# {filename}\n", encoding="utf-8")
        self.config = self.root / "config.json"
        write_json(
            self.config,
            {
                "project_root": str(self.root),
                "runtime_root": str(self.runtime),
                "python_executable": "python-test",
                "crawl": {},
                "exports": [],
            },
        )
        self.override = self.root / "songdo.json"
        write_json(
            self.override,
            [{"name": "Incheon Yeonsu Songdo", "url": "https://example.test/songdo"}],
        )

    def tearDown(self):
        self.temporary.cleanup()

    def args(self):
        return [
            "--config",
            str(self.config),
            "--source-run",
            str(self.source_run),
            "--output-root",
            str(self.runtime / "backfills"),
            "--date",
            "20260922",
            "--targets-json",
            str(self.override),
        ]

    def successful_crawler(self, calls):
        def fake_run(command, **kwargs):
            calls.append(command)
            db_path = Path(command[command.index("--db-path") + 1])
            target_path = Path(command[command.index("--targets-json") + 1])
            region_path = Path(command[command.index("--region-json-path") + 1])
            targets = json.loads(target_path.read_text(encoding="utf-8"))
            create_db(db_path)
            write_json(
                Path(str(db_path) + ".result.json"),
                valid_result(targets, backfill.sha256_file(region_path)),
            )
            return subprocess.CompletedProcess(command, 0)

        return fake_run

    def test_active_daily_lock_prevents_any_backfill_artifact_write(self):
        lock_path = self.runtime / "locks" / "daily_crawl.lock"
        write_json(
            lock_path,
            LockMetadata(
                pid=os.getpid(),
                run_id="active",
                created_at="now",
                command="orchestrator",
            ).__dict__,
        )
        with self.assertRaises(backfill.BackfillError):
            backfill.main([*self.args(), "--dry-run"])
        self.assertFalse((self.runtime / "backfills").exists())

    def test_dry_run_does_not_overwrite_existing_scoped_status(self):
        targets = json.loads(self.override.read_text(encoding="utf-8"))
        operation_dir = (
            self.runtime
            / "backfills"
            / "source_run_20260922"
            / "scopes"
            / f"explicit_{backfill.sha256_json(targets)[:12]}"
        )
        status_path = operation_dir / "status.json"
        write_json(status_path, {"status": "complete", "sentinel": True})

        self.assertEqual(backfill.main([*self.args(), "--dry-run"]), 0)
        self.assertEqual(
            json.loads(status_path.read_text(encoding="utf-8")),
            {"status": "complete", "sentinel": True},
        )

    def test_scoped_run_resumes_only_certified_batch_and_preserves_source(self):
        source_hash = backfill.sha256_file(self.source_db)
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            self.assertIn("--skip-existing-db", command)
            self.assertEqual(command[command.index("--skip-existing-db") + 1], str(self.source_db))
            db_path = Path(command[command.index("--db-path") + 1])
            target_path = Path(command[command.index("--targets-json") + 1])
            region_path = Path(command[command.index("--region-json-path") + 1])
            targets = json.loads(target_path.read_text(encoding="utf-8"))
            create_db(
                db_path,
                complexes=[(1, "priced recrawl ignored", 100), (2, "metadata rescanned", 200)],
                prices=[
                    (1, "2026-09-21", "84", 112.0, 84.0, 999, 9),
                    (2, "2026-09-22", "59", 80.0, 59.0, 200, 2),
                ],
            )
            write_json(
                Path(str(db_path) + ".result.json"),
                valid_result(targets, hashlib.sha256(region_path.read_bytes()).hexdigest()),
            )
            return subprocess.CompletedProcess(command, 0)

        with mock.patch("local.backfill.subprocess.run", side_effect=fake_run):
            self.assertEqual(backfill.main(self.args()), 0)
            self.assertEqual(backfill.main(self.args()), 0)

        self.assertEqual(len(calls), 1)
        self.assertEqual(backfill.sha256_file(self.source_db), source_hash)
        scope_dirs = list((self.runtime / "backfills").glob("*/scopes/explicit_*"))
        self.assertEqual(len(scope_dirs), 1)
        status = json.loads((scope_dirs[0] / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["selection_scope"], "explicit")
        self.assertFalse(status["nation_complete"])
        with sqlite3.connect(scope_dirs[0] / "merged.db") as connection:
            self.assertEqual(
                connection.execute(
                    "select trade_avg from prices where complex_no=1 and date='2026-09-21'"
                ).fetchone(),
                (100,),
            )
            self.assertEqual(
                connection.execute("select trade_avg from prices where complex_no=2").fetchone(),
                (200,),
            )

    def test_code_upgrade_reuses_original_proof_but_ordinary_resume_rejects(self):
        calls = []
        with mock.patch(
            "local.backfill.subprocess.run", side_effect=self.successful_crawler(calls)
        ):
            self.assertEqual(backfill.main(self.args()), 0)

        operation_dir = next(
            (self.runtime / "backfills").glob("*/scopes/explicit_*")
        )
        proof_path = operation_dir / "batches" / "batch_0000.db.complete.json"
        original_proof = proof_path.read_bytes()
        (self.root / "crawler.py").write_text("# fixed crawler\n", encoding="utf-8")

        with self.assertRaisesRegex(backfill.BackfillError, "ordinary resume refused"):
            backfill.main(self.args())
        with mock.patch(
            "local.backfill.subprocess.run", side_effect=self.successful_crawler(calls)
        ):
            self.assertEqual(
                backfill.main([*self.args(), "--accept-code-update"]), 0
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(proof_path.read_bytes(), original_proof)
        manifest = backfill.read_json(operation_dir / "inputs" / "run_manifest.json")
        self.assertEqual(len(manifest["code_generations"]), 2)
        transition = manifest["code_transitions"][-1]
        self.assertEqual(transition["validated_certified_batches"], [0])
        self.assertNotEqual(
            transition["accepted_old_code_sha256"],
            transition["new_code_sha256"],
        )

    def test_code_upgrade_never_trusts_corrupt_old_proof(self):
        calls = []
        with mock.patch(
            "local.backfill.subprocess.run", side_effect=self.successful_crawler(calls)
        ):
            self.assertEqual(backfill.main(self.args()), 0)
        operation_dir = next(
            (self.runtime / "backfills").glob("*/scopes/explicit_*")
        )
        manifest_path = operation_dir / "inputs" / "run_manifest.json"
        original_manifest = manifest_path.read_bytes()
        proof_path = operation_dir / "batches" / "batch_0000.db.complete.json"
        proof = backfill.read_json(proof_path)
        proof["db_sha256"] = "corrupt"
        write_json(proof_path, proof)
        (self.root / "crawler.py").write_text("# fixed crawler\n", encoding="utf-8")

        with mock.patch("local.backfill.subprocess.run") as run:
            with self.assertRaisesRegex(backfill.BackfillError, "prior-generation audit"):
                backfill.main([*self.args(), "--accept-code-update"])
            run.assert_not_called()
        self.assertEqual(manifest_path.read_bytes(), original_manifest)

    def test_code_upgrade_rejects_changed_source(self):
        calls = []
        with mock.patch(
            "local.backfill.subprocess.run", side_effect=self.successful_crawler(calls)
        ):
            self.assertEqual(backfill.main(self.args()), 0)
        with sqlite3.connect(self.source_db) as connection:
            connection.execute(
                "insert into complexes (complex_no, name, total_households) values (3, 'drift', 3)"
            )
        (self.root / "crawler.py").write_text("# fixed crawler\n", encoding="utf-8")

        with self.assertRaisesRegex(backfill.BackfillError, "source_db_sha256"):
            backfill.main([*self.args(), "--accept-code-update"])

    def test_scoped_exports_stay_under_scoped_operation_directory(self):
        operation_dir = self.runtime / "backfills" / "source_run_20260922" / "scopes" / "explicit_test"
        merged_db = operation_dir / "merged.db"
        create_db(merged_db)
        config = {
            "exports": [
                {"name": "Songdo", "regions": ["인천시"], "min_households": 100}
            ]
        }

        def fake_logged(command, log_path, cwd, env=None):
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("fixture", encoding="utf-8")
            return 0

        with mock.patch("local.backfill.run_logged", side_effect=fake_logged):
            generated = backfill._export_jobs(
                config,
                self.root,
                "python-test",
                operation_dir / "exports",
                merged_db,
            )

        self.assertEqual(len(generated), 1)
        self.assertTrue(Path(generated[0]).is_relative_to(operation_dir / "exports"))
        self.assertFalse((self.runtime / "backfills" / "source_run_20260922" / "exports").exists())


if __name__ == "__main__":
    unittest.main()
