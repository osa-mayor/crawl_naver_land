from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from local.config import LocalConfig, load_config
from local.lock import LockError, file_lock


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
    return now_kst().strftime("%Y%m%d_%H%M%S_kst")


def default_summary(run_id: str, run_dir: Path) -> dict:
    started_at = now_kst().isoformat()
    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": None,
        "status": "running",
        "error_stage": None,
        "run_dir": str(run_dir),
        "completed_shards": [],
        "failed_shards": [],
        "db_count": 0,
        "merged_db_path": None,
        "excel_files": [],
        "google_drive_uploads": [],
        "discord_notification": None,
        "groups": [],
    }


class PipelineError(RuntimeError):
    pass


def load_token_text(token_file: Path) -> str:
    with token_file.open("r", encoding="utf-8") as f:
        return f.read().strip()


def load_webhook_text(webhook_file: Path) -> str:
    with webhook_file.open("r", encoding="utf-8") as f:
        return f.read().strip()


def build_runtime_paths(config: LocalConfig, run_id: str) -> dict[str, Path]:
    runtime_root = ensure_dir(config.runtime_root)
    run_dir = ensure_dir(runtime_root / "runs" / run_id)
    paths = {
        "runtime_root": runtime_root,
        "run_dir": run_dir,
        "logs": ensure_dir(run_dir / "logs"),
        "shards": ensure_dir(run_dir / "shards"),
        "exports": ensure_dir(run_dir / "exports"),
        "screenshots": ensure_dir(run_dir / "screenshots"),
        "locks": ensure_dir(runtime_root / "locks"),
        "system_logs": ensure_dir(runtime_root / "logs"),
        "summary": run_dir / "summary.json",
        "failed_shards": run_dir / "failed_shards.txt",
        "merged_db": run_dir / "merged.db",
    }
    return paths


def run_command(
    command: list[str],
    log_path: Path,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess:
    ensure_dir(log_path.parent)
    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write("COMMAND: " + " ".join(command) + "\n\n")
        if timeout_seconds is not None:
            log_file.write(f"TIMEOUT_SECONDS: {timeout_seconds}\n\n")
        log_file.flush()

        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            log_file.write(
                f"\nTIMEOUT: command exceeded {timeout_seconds} seconds; "
                "terminating process group.\n"
            )
            log_file.flush()
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=30)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()

        return subprocess.CompletedProcess(command, process.returncode)


def build_base_env(config: LocalConfig, paths: dict[str, Path]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "MAX_CONCURRENT_PAGES": str(config.crawl.max_concurrent_pages),
            "MAX_API_PREFETCH_CONCURRENCY": str(
                config.crawl.max_api_prefetch_concurrency
            ),
            "NAVER_REGION_JSON_PATH": str(config.region_json_path),
            "CRAWLER_SCREENSHOT_DIR": str(paths["screenshots"]),
        }
    )
    return env


def preflight(config: LocalConfig, paths: dict[str, Path]) -> None:
    required_files = [
        config.project_root / "crawler.py",
        config.project_root / "merge_db.py",
        config.project_root / "export_db.py",
        config.project_root / "upload_drive.py",
        config.project_root / "send_discord.py",
        config.region_json_path,
    ]
    missing = [str(path) for path in required_files if not path.exists()]
    if missing:
        raise PipelineError("missing required files: " + ", ".join(missing))

    if config.google_drive.enabled:
        if not config.google_drive.folder_id:
            raise PipelineError("google drive folder_id is empty")
        if not config.google_drive.token_file.exists():
            raise PipelineError(
                f"google drive token file not found: {config.google_drive.token_file}"
            )
        load_token_text(config.google_drive.token_file)

    if config.discord.enabled:
        if config.discord.webhook_url:
            pass
        elif config.discord.webhook_file.exists() and load_webhook_text(
            config.discord.webhook_file
        ):
            pass
        else:
            raise PipelineError("discord is enabled but no webhook is configured")

    if config.preflight_smoke_test:
        env = build_base_env(config, paths)
        result = run_command(
            [
                config.python_executable,
                str(config.project_root / "test_naver_connection.py"),
            ],
            paths["logs"] / "preflight_naver_connection.log",
            config.project_root,
            env,
        )
        if result.returncode != 0:
            raise PipelineError("preflight smoke test failed")


def parse_group(group: str) -> list[int]:
    start_str, end_str = group.split("-", 1)
    start = int(start_str)
    end = int(end_str)
    return list(range(start, end + 1))


def run_shard(
    config: LocalConfig, paths: dict[str, Path], shard_index: int
) -> tuple[bool, dict]:
    shard_db = paths["shards"] / f"db_shard_{shard_index}.db"
    shard_log = paths["logs"] / f"shard_{shard_index}.log"
    env = build_base_env(config, paths)
    env["CRAWLER_LOG_PATH"] = str(paths["logs"] / f"crawler_shard_{shard_index}.log")

    command = [
        config.python_executable,
        str(config.project_root / "crawler.py"),
        "--shard-index",
        str(shard_index),
        "--shard-total",
        str(config.crawl.shard_total),
        "--db-path",
        str(shard_db),
        "--log-path",
        env["CRAWLER_LOG_PATH"],
        "--screenshot-dir",
        str(paths["screenshots"]),
        "--region-json-path",
        str(config.region_json_path),
    ]

    attempts = 0
    max_attempts = config.crawl.retry_failed_shards + 1
    while attempts < max_attempts:
        attempts += 1
        result = run_command(
            command,
            shard_log,
            config.project_root,
            env,
            timeout_seconds=config.crawl.shard_timeout_minutes * 60,
        )
        if result.returncode == 0 and shard_db.exists():
            return True, {
                "shard": shard_index,
                "attempts": attempts,
                "db_path": str(shard_db),
                "log_path": str(shard_log),
            }

    return False, {
        "shard": shard_index,
        "attempts": attempts,
        "db_path": str(shard_db),
        "log_path": str(shard_log),
    }


def run_group(config: LocalConfig, paths: dict[str, Path], group: str) -> dict:
    shard_results = []
    completed = []
    failed = []

    for shard_index in parse_group(group):
        ok, payload = run_shard(config, paths, shard_index)
        shard_results.append(payload)
        if ok:
            completed.append(shard_index)
        else:
            failed.append(shard_index)

    return {
        "group": group,
        "completed_shards": completed,
        "failed_shards": failed,
        "shards": shard_results,
    }


def write_failed_shards(path: Path, failed_shards: list[int]) -> None:
    if not failed_shards:
        if path.exists():
            path.unlink()
        return
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        for shard in failed_shards:
            f.write(f"{shard}\n")


def run_merge(config: LocalConfig, paths: dict[str, Path]) -> Path:
    pattern = str(paths["shards"] / "db_shard_*.db")
    command = [
        config.python_executable,
        str(config.project_root / "merge_db.py"),
        pattern,
        str(paths["merged_db"]),
    ]
    result = run_command(command, paths["logs"] / "merge.log", config.project_root)
    if result.returncode != 0 or not paths["merged_db"].exists():
        raise PipelineError("merge step failed")
    return paths["merged_db"]


def run_exports(config: LocalConfig, paths: dict[str, Path]) -> list[str]:
    date_str = now_kst().strftime("%Y-%m-%d")
    generated = []
    for export_job in config.export_jobs:
        output_path = paths["exports"] / f"export_{date_str}_{export_job.name}.xlsx"
        command = [
            config.python_executable,
            str(config.project_root / "export_db.py"),
            str(paths["merged_db"]),
            "--output",
            str(output_path),
            "--min-households",
            str(export_job.min_households),
            "--region",
            *export_job.regions,
        ]
        result = run_command(
            command,
            paths["logs"] / f"export_{export_job.name}.log",
            config.project_root,
        )
        if result.returncode == 0 and output_path.exists():
            generated.append(str(output_path))
    return generated


def upload_files(
    config: LocalConfig, paths: dict[str, Path], files: list[Path]
) -> list[dict]:
    uploads = []
    if not config.google_drive.enabled:
        return uploads

    for file_path in files:
        command = [
            config.python_executable,
            str(config.project_root / "upload_drive.py"),
            "--files",
            str(file_path),
            "--folder",
            config.google_drive.folder_id,
            "--token-file",
            str(config.google_drive.token_file),
        ]
        result = run_command(
            command,
            paths["logs"] / f"upload_{file_path.name}.log",
            config.project_root,
        )
        uploads.append(
            {
                "file": str(file_path),
                "success": result.returncode == 0,
            }
        )
    return uploads


def build_discord_message(summary: dict) -> str:
    run_id = summary["run_id"]
    if summary["status"] == "success":
        return f"✅ [Local Crawl Success] {run_id} completed. DB count={summary['db_count']}, excel files={len(summary['excel_files'])}"
    if summary["status"] == "partial_success":
        return f"⚠️ [Local Crawl Partial Success] {run_id} completed with failed shards={summary['failed_shards']}"
    if summary["status"] == "skipped_due_to_lock":
        return (
            f"⏭️ [Local Crawl Skipped] {run_id} skipped because another run is active."
        )
    return f"🚨 [Local Crawl Failure] {run_id} failed at stage={summary['error_stage']}"


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

    message = build_discord_message(summary)
    command = [
        config.python_executable,
        str(config.project_root / "send_discord.py"),
        "--message",
        message,
        "--webhook",
        webhook_url,
    ]
    result = run_command(command, paths["logs"] / "discord.log", config.project_root)
    return {"success": result.returncode == 0, "message": message}


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


def finalize_summary(summary: dict, path: Path) -> None:
    summary["finished_at"] = now_kst().isoformat()
    write_json(path, summary)


def run_pipeline(config: LocalConfig, run_id: str) -> int:
    paths = build_runtime_paths(config, run_id)
    summary = default_summary(run_id, paths["run_dir"])
    write_json(paths["summary"], summary)

    latest_link = paths["runtime_root"] / "latest"

    try:
        preflight(config, paths)

        with file_lock(
            paths["locks"] / "daily_crawl.lock", run_id, "local.orchestrator"
        ):
            with ThreadPoolExecutor(
                max_workers=config.crawl.max_local_parallel
            ) as executor:
                futures = {
                    executor.submit(run_group, config, paths, group): group
                    for group in config.crawl.shard_groups
                }
                for future in as_completed(futures):
                    group_result = future.result()
                    summary["groups"].append(group_result)
                    summary["completed_shards"].extend(group_result["completed_shards"])
                    summary["failed_shards"].extend(group_result["failed_shards"])
                    write_json(paths["summary"], summary)

            summary["completed_shards"] = sorted(set(summary["completed_shards"]))
            summary["failed_shards"] = sorted(set(summary["failed_shards"]))
            summary["db_count"] = len(list(paths["shards"].glob("db_shard_*.db")))
            write_failed_shards(paths["failed_shards"], summary["failed_shards"])

            if summary["db_count"] == 0:
                raise PipelineError("no shard databases were created")

            summary["merged_db_path"] = str(run_merge(config, paths))
            summary["excel_files"] = run_exports(config, paths)

            upload_targets = [paths["merged_db"]] + [
                Path(path) for path in summary["excel_files"]
            ]
            summary["google_drive_uploads"] = upload_files(
                config, paths, upload_targets
            )

            if summary["failed_shards"]:
                summary["status"] = "partial_success"
            else:
                summary["status"] = "success"

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
    parser = argparse.ArgumentParser(description="Run local crawl pipeline on Mac mini")
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
