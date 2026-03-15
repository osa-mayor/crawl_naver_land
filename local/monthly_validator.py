from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from local.config import LocalConfig, load_config
from local.lock import LockError, file_lock


class PipelineError(RuntimeError):
    pass


def now_kst() -> datetime:
    return datetime.utcnow() + timedelta(hours=9)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: dict) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def make_run_id() -> str:
    return now_kst().strftime("monthly_validator_%Y%m%d_%H%M%S_kst")


def run_command(
    command: list[str], log_path: Path, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    ensure_dir(log_path.parent)
    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write("COMMAND: " + " ".join(command) + "\n\n")
        log_file.flush()
        return subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )


def load_webhook_text(webhook_file: Path) -> str:
    with webhook_file.open("r", encoding="utf-8") as f:
        return f.read().strip()


def build_runtime_paths(config: LocalConfig, run_id: str) -> dict[str, Path]:
    runtime_root = ensure_dir(config.validator.runtime_root)
    run_dir = ensure_dir(runtime_root / "runs" / run_id)
    return {
        "runtime_root": runtime_root,
        "run_dir": run_dir,
        "logs": ensure_dir(run_dir / "logs"),
        "locks": ensure_dir(runtime_root / "locks"),
        "summary": run_dir / "summary.json",
    }


def default_summary(run_id: str, run_dir: Path) -> dict:
    return {
        "run_id": run_id,
        "started_at": now_kst().isoformat(),
        "finished_at": None,
        "status": "running",
        "error_stage": None,
        "run_dir": str(run_dir),
        "region_file": None,
        "validator_changed_file": False,
        "commit_created": False,
        "commit_sha": None,
        "push_attempted": False,
        "push_succeeded": False,
        "discord_notification": None,
    }


def git_command(
    repo_root: Path,
    args: list[str],
    log_path: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    command = ["git", *args]
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return run_command(command, log_path, repo_root, env)


def git_stdout(repo_root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise PipelineError(proc.stderr.strip() or "git command failed")
    return proc.stdout.strip()


def preflight(config: LocalConfig, paths: dict[str, Path]) -> None:
    required = [
        config.validator.repo_root / ".git",
        config.validator.repo_root / "region_validator.py",
        config.validator.region_file,
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise PipelineError("missing required files: " + ", ".join(missing))

    branch = git_stdout(config.validator.repo_root, ["branch", "--show-current"])
    if branch != config.validator.expected_branch:
        raise PipelineError(
            f"unexpected branch: {branch} (expected {config.validator.expected_branch})"
        )

    status = git_stdout(config.validator.repo_root, ["status", "--porcelain"])
    if status:
        raise PipelineError("monthly validator requires a clean git worktree")

    remote = git_stdout(config.validator.repo_root, ["remote"])
    remotes = {line.strip() for line in remote.splitlines() if line.strip()}
    if config.validator.remote_name not in remotes:
        raise PipelineError(f"missing git remote: {config.validator.remote_name}")

    if config.discord.enabled:
        webhook_url = config.discord.webhook_url
        if not webhook_url and config.discord.webhook_file.exists():
            webhook_url = load_webhook_text(config.discord.webhook_file)
        if not webhook_url:
            raise PipelineError("discord is enabled but no webhook is configured")


def build_validator_env(config: LocalConfig) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "REGION_FILE": str(config.validator.region_file),
            "MAX_CONCURRENT_TABS": str(config.validator.max_concurrent_tabs),
            "REGION_VALIDATOR_TIMEOUT_MS": str(config.validator.timeout_ms),
        }
    )
    return env


def notify_discord(
    config: LocalConfig, paths: dict[str, Path], summary: dict
) -> dict | None:
    if not config.discord.enabled:
        return None

    webhook_url = config.discord.webhook_url
    if not webhook_url and config.discord.webhook_file.exists():
        webhook_url = load_webhook_text(config.discord.webhook_file)
    if not webhook_url:
        return {"success": False, "message": "discord webhook missing"}

    run_id = summary["run_id"]
    if summary["status"] == "success":
        message = f"✅ [Monthly Region Validator] {run_id} updated and pushed {Path(summary['region_file']).name}"
    elif summary["status"] == "no_change":
        message = f"ℹ️ [Monthly Region Validator] {run_id} completed with no changes"
    elif summary["status"] == "skipped_due_to_lock":
        message = f"⏭️ [Monthly Region Validator] {run_id} skipped because another validator run is active"
    else:
        message = f"🚨 [Monthly Region Validator] {run_id} failed at stage={summary['error_stage']}"

    command = [
        config.python_executable,
        str(config.project_root / "send_discord.py"),
        "--message",
        message,
        "--webhook",
        webhook_url,
    ]
    result = run_command(
        command,
        paths["logs"] / "discord.log",
        config.validator.repo_root,
    )
    return {"success": result.returncode == 0, "message": message}


def finalize_summary(summary: dict, path: Path) -> None:
    summary["finished_at"] = now_kst().isoformat()
    write_json(path, summary)


def cleanup_old_runs(config: LocalConfig, paths: dict[str, Path]) -> None:
    runs_root = paths["runtime_root"] / "runs"
    if not runs_root.exists():
        return
    cutoff = now_kst() - timedelta(days=config.retention_days)
    for child in runs_root.iterdir():
        if not child.is_dir():
            continue
        modified = datetime.fromtimestamp(child.stat().st_mtime)
        if modified < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def run_validator(config: LocalConfig, paths: dict[str, Path], summary: dict) -> None:
    env = build_validator_env(config)
    command = [
        config.python_executable,
        str(config.validator.repo_root / "region_validator.py"),
    ]
    result = run_command(
        command,
        paths["logs"] / "validator.log",
        config.validator.repo_root,
        env,
    )
    if result.returncode != 0:
        raise PipelineError("region validator failed")


def has_region_file_change(config: LocalConfig) -> bool:
    rel_path = str(config.validator.region_file.relative_to(config.validator.repo_root))
    status = git_stdout(
        config.validator.repo_root, ["status", "--porcelain", "--", rel_path]
    )
    return bool(status.strip())


def commit_and_push(config: LocalConfig, paths: dict[str, Path], summary: dict) -> None:
    rel_path = str(config.validator.region_file.relative_to(config.validator.repo_root))
    stage = git_command(
        config.validator.repo_root,
        ["add", rel_path],
        paths["logs"] / "git_add.log",
    )
    if stage.returncode != 0:
        raise PipelineError("git add failed")

    if not has_region_file_change(config):
        summary["status"] = "no_change"
        return

    commit_message = config.validator.commit_message_template.format(
        date=now_kst().strftime("%Y-%m-%d")
    )
    commit = git_command(
        config.validator.repo_root,
        [
            "-c",
            f"user.name={config.validator.commit_name}",
            "-c",
            f"user.email={config.validator.commit_email}",
            "commit",
            "-m",
            commit_message,
            "--",
            rel_path,
        ],
        paths["logs"] / "git_commit.log",
    )
    if commit.returncode != 0:
        raise PipelineError("git commit failed")

    summary["commit_created"] = True
    summary["commit_sha"] = git_stdout(
        config.validator.repo_root, ["rev-parse", "HEAD"]
    )

    if not config.validator.push_changes:
        summary["status"] = "success"
        return

    summary["push_attempted"] = True
    push = git_command(
        config.validator.repo_root,
        ["push", config.validator.remote_name, config.validator.expected_branch],
        paths["logs"] / "git_push.log",
    )
    summary["push_succeeded"] = push.returncode == 0
    if push.returncode != 0:
        raise PipelineError("git push failed")

    summary["status"] = "success"


def run_pipeline(config: LocalConfig, run_id: str) -> int:
    paths = build_runtime_paths(config, run_id)
    summary = default_summary(run_id, paths["run_dir"])
    summary["region_file"] = str(config.validator.region_file)
    write_json(paths["summary"], summary)

    latest_link = paths["runtime_root"] / "latest"

    try:
        preflight(config, paths)
        with file_lock(
            paths["locks"] / "monthly_validator.lock",
            run_id,
            "local.monthly_validator",
        ):
            run_validator(config, paths, summary)
            summary["validator_changed_file"] = has_region_file_change(config)
            if summary["validator_changed_file"]:
                commit_and_push(config, paths, summary)
            else:
                summary["status"] = "no_change"
            summary["discord_notification"] = notify_discord(config, paths, summary)
    except LockError as exc:
        summary["status"] = "skipped_due_to_lock"
        summary["error_stage"] = str(exc)
        summary["discord_notification"] = notify_discord(config, paths, summary)
        finalize_summary(summary, paths["summary"])
        return 0
    except PipelineError as exc:
        summary["status"] = "failure"
        summary["error_stage"] = str(exc)
        summary["discord_notification"] = notify_discord(config, paths, summary)
        finalize_summary(summary, paths["summary"])
        return 1
    except Exception as exc:
        summary["status"] = "failure"
        summary["error_stage"] = repr(exc)
        summary["discord_notification"] = notify_discord(config, paths, summary)
        finalize_summary(summary, paths["summary"])
        return 1

    if latest_link.exists() or latest_link.is_symlink():
        latest_link.unlink()
    latest_link.symlink_to(paths["run_dir"], target_is_directory=True)
    cleanup_old_runs(config, paths)
    finalize_summary(summary, paths["summary"])
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local monthly region validator")
    parser.add_argument(
        "--config", default="local/config.json", help="Path to local config file"
    )
    parser.add_argument("--run-id", help="Optional run id override")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    run_id = args.run_id or make_run_id()
    return run_pipeline(config, run_id)


if __name__ == "__main__":
    raise SystemExit(main())
