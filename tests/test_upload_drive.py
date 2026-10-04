import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from local import orchestrator
from upload_drive import build_upload_name


class UploadNameTests(unittest.TestCase):
    def test_explicit_run_date_keeps_remote_name_stable(self):
        self.assertEqual(
            build_upload_name("/tmp/merged.db", date_prefix="2026-08-25"),
            "2026-08-25_merged.db",
        )

    def test_successful_identical_upload_is_checkpointed_without_network_call(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file_path = root / "merged.db"
            file_path.write_bytes(b"stable")
            previous = {
                "file": str(file_path),
                "success": True,
                "size_bytes": file_path.stat().st_size,
                "sha256": orchestrator.sha256_file(file_path),
                "date_prefix": "2026-08-25",
            }
            config = SimpleNamespace(
                google_drive=SimpleNamespace(enabled=True),
                crawl=SimpleNamespace(retry_failed_shards=2),
            )
            checkpoints = []

            with patch("local.orchestrator.run_command") as run_command:
                uploads = orchestrator.upload_files(
                    config,
                    {"logs": root},
                    [file_path],
                    date_prefix="2026-08-25",
                    previous_uploads=[previous],
                    checkpoint=lambda items: checkpoints.append(list(items)),
                )

            run_command.assert_not_called()
            self.assertTrue(uploads[0]["skipped_duplicate"])
            self.assertEqual(checkpoints[-1], uploads)


if __name__ == "__main__":
    unittest.main()
