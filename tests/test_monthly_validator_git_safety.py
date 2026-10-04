import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from local import monthly_validator


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def make_config(repo: Path, *, push_changes: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        validator=SimpleNamespace(
            repo_root=repo,
            region_file=repo / "naver_region_codes.json",
            expected_branch="main",
            remote_name="origin",
            commit_message_template="Update region validity: {date}",
            commit_name="Local Validator",
            commit_email="local-validator@localhost",
            push_changes=push_changes,
        ),
        discord=SimpleNamespace(enabled=False),
    )


class MonthlyValidatorGitSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        git(self.repo, "init", "-b", "main")
        git(self.repo, "remote", "add", "origin", "https://example.invalid/repo.git")
        (self.repo / "naver_region_codes.json").write_text("{}\n", encoding="utf-8")
        (self.repo / "region_validator.py").write_text("# validator\n", encoding="utf-8")
        (self.repo / "unrelated.py").write_text("before\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(
            self.repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        )
        self.config = make_config(self.repo)
        self.paths = {"logs": self.repo / "logs"}

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_preflight_allows_unrelated_unstaged_and_untracked_changes(self):
        (self.repo / "unrelated.py").write_text("after\n", encoding="utf-8")
        (self.repo / "new_test.py").write_text("new\n", encoding="utf-8")

        monthly_validator.preflight(self.config, self.paths)

    def test_preflight_rejects_existing_region_file_change(self):
        (self.repo / "naver_region_codes.json").write_text(
            '{"changed": true}\n', encoding="utf-8"
        )

        with self.assertRaisesRegex(
            monthly_validator.PipelineError, "region file must be clean"
        ):
            monthly_validator.preflight(self.config, self.paths)

    def test_preflight_rejects_preexisting_staged_changes(self):
        (self.repo / "unrelated.py").write_text("staged\n", encoding="utf-8")
        git(self.repo, "add", "unrelated.py")

        with self.assertRaisesRegex(
            monthly_validator.PipelineError, "git index must be clean"
        ):
            monthly_validator.preflight(self.config, self.paths)

    def test_commit_records_only_region_file(self):
        (self.repo / "naver_region_codes.json").write_text(
            '{"validated": true}\n', encoding="utf-8"
        )
        (self.repo / "unrelated.py").write_text("local work\n", encoding="utf-8")
        summary = {"status": "running"}

        monthly_validator.commit_and_push(
            self.config,
            self.paths,
            summary,
        )

        changed_in_commit = git(
            self.repo, "show", "--pretty=format:", "--name-only", "HEAD"
        ).stdout.splitlines()
        self.assertEqual(changed_in_commit, ["naver_region_codes.json"])
        self.assertIn(" M unrelated.py", git(self.repo, "status", "--short").stdout)
        self.assertTrue(summary["commit_created"])
        self.assertEqual(summary["status"], "success")


if __name__ == "__main__":
    unittest.main()
