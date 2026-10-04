from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
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
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        "merged_db_quality": None,
        "excel_files": [],
        "google_drive_uploads": [],
        "discord_notification": None,
        "groups": [],
        "shard_results": {},
        "stage": "initializing",
        "resume_count": 0,
    }


class PipelineError(RuntimeError):
    pass


def append_log(path: Path, message: str) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(message.rstrip() + "\n")


def can_connect(host: str, port: int = 443, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_network_ready(
    hosts: list[str],
    log_path: Path,
    attempts: int = 12,
    delay_seconds: float = 5.0,
) -> bool:
    for attempt in range(1, attempts + 1):
        failed_hosts = [host for host in hosts if not can_connect(host)]
        if not failed_hosts:
            return True

        append_log(
            log_path,
            (
                f"NETWORK_WAIT: failed hosts={failed_hosts} "
                f"attempt={attempt}/{attempts}; sleeping {delay_seconds}s"
            ),
        )
        time.sleep(delay_seconds)

    append_log(log_path, f"NETWORK_FAILED: hosts did not recover: {hosts}")
    return False


def load_token_text(token_file: Path) -> str:
    with token_file.open("r", encoding="utf-8") as f:
        return f.read().strip()


def load_webhook_text(webhook_file: Path) -> str:
    with webhook_file.open("r", encoding="utf-8") as f:
        return f.read().strip()


def build_runtime_paths(
    config: LocalConfig, run_id: str, create: bool = True
) -> dict[str, Path]:
    runtime_root = config.runtime_root
    run_dir = runtime_root / "runs" / run_id
    paths = {
        "runtime_root": runtime_root,
        "run_dir": run_dir,
        "logs": run_dir / "logs",
        "shards": run_dir / "shards",
        "exports": run_dir / "exports",
        "screenshots": run_dir / "screenshots",
        "inputs": run_dir / "inputs",
        "locks": runtime_root / "locks",
        "system_logs": runtime_root / "logs",
        "summary": run_dir / "summary.json",
        "failed_shards": run_dir / "failed_shards.txt",
        "merged_db": run_dir / "merged.db",
        "region_snapshot": run_dir / "inputs" / "naver_region_codes.json",
    }
    if create:
        for key in (
            "runtime_root",
            "run_dir",
            "logs",
            "shards",
            "exports",
            "screenshots",
            "inputs",
            "locks",
            "system_logs",
        ):
            ensure_dir(paths[key])
    return paths


def prepare_run_inputs(
    config: LocalConfig, paths: dict[str, Path], summary: dict, resume: bool
) -> None:
    snapshot = paths["region_snapshot"]
    if resume:
        required_metadata = (
            "region_json_sha256",
            "region_json_snapshot",
            "shard_total",
            "configured_shard_groups",
        )
        missing = [name for name in required_metadata if name not in summary]
        if missing or not snapshot.exists():
            raise PipelineError(
                "legacy resume is missing original snapshot/shard metadata: "
                + ", ".join(missing or ["region snapshot"])
            )
        recorded_snapshot = Path(str(summary["region_json_snapshot"]))
        if recorded_snapshot.resolve() != snapshot.resolve():
            raise PipelineError(
                "run region snapshot path changed; refusing unsafe shard resume"
            )
    elif not snapshot.exists():
        shutil.copy2(config.region_json_path, snapshot)

    snapshot_hash = sha256_file(snapshot)
    previous_hash = summary.get("region_json_sha256")
    if previous_hash and previous_hash != snapshot_hash:
        raise PipelineError(
            "run region snapshot hash changed; refusing unsafe shard resume"
        )

    previous_total = summary.get("shard_total")
    if resume:
        if previous_total != config.crawl.shard_total:
            raise PipelineError(
                "shard_total changed since run start; refusing unsafe resume"
            )

    previous_groups = summary.get("configured_shard_groups")
    if resume:
        if list(previous_groups) != list(config.crawl.shard_groups):
            raise PipelineError(
                "shard_groups changed since run start; refusing unsafe resume"
            )

    summary["region_json_sha256"] = snapshot_hash
    summary["region_json_snapshot"] = str(snapshot)
    summary["shard_total"] = config.crawl.shard_total
    summary["configured_shard_groups"] = list(config.crawl.shard_groups)
    paths["region_json"] = snapshot
    paths["region_json_sha256"] = snapshot_hash


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
            "CRAWLER_HEADLESS": "1" if getattr(config.crawl, "headless", True) else "0",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "MAX_CONCURRENT_PAGES": str(config.crawl.max_concurrent_pages),
            "MAX_API_PREFETCH_CONCURRENCY": str(
                config.crawl.max_api_prefetch_concurrency
            ),
            "NAVIGATION_RETRY_ATTEMPTS": str(config.crawl.navigation_retry_attempts),
            "NETWORK_READY_RETRY_ATTEMPTS": str(
                config.crawl.network_ready_retry_attempts
            ),
            "NETWORK_READY_RETRY_DELAY_SECONDS": str(
                config.crawl.network_ready_retry_delay_seconds
            ),
            "MAX_FAILED_REGION_RATIO": str(config.crawl.max_failed_region_ratio),
            "MAX_RATE_LIMIT_RESPONSES": str(config.crawl.max_rate_limit_responses),
            "ARTICLE_RESPONSE_TIMEOUT_MS": str(
                config.crawl.article_response_timeout_ms
            ),
            "MAX_ARTICLE_PAGES_PER_TRADE": str(
                config.crawl.max_article_pages_per_trade
            ),
            "MAX_ARTICLE_MISSING_COMPLEX_RATIO": str(
                config.crawl.max_article_missing_complex_ratio
            ),
            "MIN_COMPLEXES_FOR_ARTICLE_CHECK": str(
                config.crawl.min_complexes_for_article_check
            ),
            "NAVER_REGION_JSON_PATH": str(
                paths.get("region_json", config.region_json_path)
            ),
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
                str(config.project_root / "crawler.py"),
                "--inventory-preflight",
                "--region-json-path",
                str(paths.get("region_json", config.region_json_path)),
                "--log-path",
                str(paths["logs"] / "preflight_inventory.log"),
            ],
            paths["logs"] / "preflight_naver_connection.log",
            config.project_root,
            env,
            timeout_seconds=120,
        )
        if result.returncode != 0:
            raise PipelineError("preflight region inventory failed; refusing crawl")


def parse_group(group: str) -> list[int]:
    match = re.fullmatch(r"(-?\d+)-(-?\d+)", str(group).strip())
    if not match:
        raise PipelineError(f"invalid shard group: {group!r}")
    start, end = (int(value) for value in match.groups())
    if start < 0 or end < 0 or start > end:
        raise PipelineError(f"invalid shard group range: {group!r}")
    return list(range(start, end + 1))


def shard_result_path(shard_db: Path) -> Path:
    return Path(str(shard_db) + ".result.json")


def shard_completion_path(shard_db: Path) -> Path:
    return Path(str(shard_db) + ".complete.json")


def archive_incomplete_shard_db(shard_db: Path, reason: str) -> None:
    artifacts = [
        path
        for path in (
            shard_db,
            shard_result_path(shard_db),
            shard_completion_path(shard_db),
        )
        if path.exists()
    ]
    if not artifacts:
        return
    archive_root = ensure_dir(shard_db.parent / "archive")
    safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "_", reason)
    timestamp = now_kst().strftime("%Y%m%d_%H%M%S_%f")
    archive_dir = archive_root / f"{shard_db.stem}.{safe_reason}.{timestamp}"
    suffix = 0
    while archive_dir.exists():
        suffix += 1
        archive_dir = archive_root / (
            f"{shard_db.stem}.{safe_reason}.{timestamp}.{suffix}"
        )
    archive_dir.mkdir()
    for artifact in artifacts:
        artifact.rename(archive_dir / artifact.name)


def shard_price_count(shard_db: Path) -> int:
    if not shard_db.exists():
        return 0
    try:
        with sqlite3.connect(shard_db) as conn:
            row = conn.execute("select count(*) from prices").fetchone()
            return int(row[0] if row else 0)
    except sqlite3.Error:
        return 0


def inspect_shard_db(shard_db: Path) -> dict | None:
    if not shard_db.exists():
        return None
    try:
        with sqlite3.connect(f"file:{shard_db}?mode=ro", uri=True) as conn:
            integrity_row = conn.execute("pragma quick_check").fetchone()
            if not integrity_row or integrity_row[0] != "ok":
                return None
            tables = {
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type = 'table'"
                ).fetchall()
            }
            if not {"complexes", "prices"}.issubset(tables):
                return None
            complex_count = int(
                conn.execute("select count(*) from complexes").fetchone()[0] or 0
            )
            price_count = int(
                conn.execute("select count(*) from prices").fetchone()[0] or 0
            )
            return {
                "complex_count": complex_count,
                "price_count": price_count,
                "size_bytes": shard_db.stat().st_size,
            }
    except (OSError, sqlite3.Error):
        return None


def expected_shard_targets(
    region_snapshot: Path, shard_index: int, shard_total: int
) -> list[dict[str, str]]:
    if shard_total <= 0 or shard_index < 0 or shard_index >= shard_total:
        raise PipelineError("invalid shard index/total")
    try:
        data = read_json(region_snapshot)
    except (OSError, ValueError) as exc:
        raise PipelineError(f"region snapshot could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise PipelineError("region snapshot root must be an object")

    leaf_targets: list[dict[str, str]] = []

    def collect(node: object, current_name: str) -> None:
        if not isinstance(node, dict):
            raise PipelineError(f"invalid region node: {current_name}")
        children = node.get("children")
        if children:
            if not isinstance(children, dict):
                raise PipelineError(f"invalid region children: {current_name}")
            for child_name, child in children.items():
                collect(child, f"{current_name} {child_name}".strip())
            return
        if "url" in node and node.get("has_complexes") is not False:
            url = node["url"]
            if not isinstance(url, str) or not url.strip():
                raise PipelineError(f"invalid leaf target URL: {current_name}")
            leaf_targets.append({"name": current_name, "url": url})

    for root_name, root_node in data.items():
        collect(root_node, str(root_name))

    deduplicated: list[dict[str, str]] = []
    seen_urls: dict[str, str] = {}
    for target in leaf_targets:
        previous_name = seen_urls.get(target["url"])
        if previous_name is not None:
            if previous_name != target["name"]:
                raise PipelineError(
                    "duplicate leaf region URL has conflicting names: "
                    f"{previous_name!r} / {target['name']!r}"
                )
            continue
        seen_urls[target["url"]] = target["name"]
        deduplicated.append(target)
    deduplicated.sort(key=lambda row: row["name"])
    if not deduplicated:
        raise PipelineError("region snapshot contains no validated leaf targets")

    shard_targets = deduplicated[shard_index::shard_total]
    if not shard_targets:
        raise PipelineError(f"shard {shard_index} has no expected leaf targets")
    return shard_targets


def target_set_sha256(targets: list[dict[str, str]]) -> str:
    canonical = sorted(
        ({"name": target["name"], "url": target["url"]} for target in targets),
        key=lambda target: (target["name"], target["url"]),
    )
    encoded = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_crawler_result(
    payload: dict,
    expected_region_hash: str,
    expected_targets: list[dict[str, str]],
) -> dict:
    if not isinstance(payload, dict):
        raise PipelineError("crawler result must be a JSON object")
    if payload.get("schema_version") != 1:
        raise PipelineError("crawler result schema_version must be 1")
    if payload.get("status") != "complete":
        raise PipelineError("crawler result did not report complete status")
    if payload.get("mode") != "full":
        raise PipelineError("normal pipeline requires crawler result mode='full'")
    if payload.get("saved") is not True:
        raise PipelineError("crawler result did not confirm a committed save")
    if payload.get("region_json_sha256") != expected_region_hash:
        raise PipelineError("crawler result region snapshot hash mismatch")

    expected_count = payload.get("expected_target_count")
    failed_count = payload.get("failed_target_count")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise PipelineError("crawler result expected_target_count must be an integer")
    if expected_count <= 0:
        raise PipelineError("crawler result must contain at least one expected target")
    if isinstance(failed_count, bool) or not isinstance(failed_count, int):
        raise PipelineError("crawler result failed_target_count must be an integer")

    targets = payload.get("targets")
    if not isinstance(targets, list) or len(targets) != expected_count:
        raise PipelineError("crawler result target count mismatch")
    urls: set[str] = set()
    actual_failed = 0
    for target in targets:
        if not isinstance(target, dict):
            raise PipelineError("crawler result target must be an object")
        name = target.get("name")
        url = target.get("url")
        status = target.get("status")
        if not isinstance(name, str) or not name.strip():
            raise PipelineError("crawler result target name is missing")
        if not isinstance(url, str) or not url.strip():
            raise PipelineError("crawler result target URL is missing")
        if url in urls:
            raise PipelineError("crawler result contains duplicate target URLs")
        urls.add(url)
        if status not in {"complete", "empty", "failed"}:
            raise PipelineError(f"crawler result target has invalid status: {status!r}")
        if status == "failed":
            actual_failed += 1

    if failed_count != actual_failed:
        raise PipelineError("crawler result failed target count mismatch")
    if failed_count:
        raise PipelineError("crawler result contains failed targets")
    actual_target_set = {(target["name"], target["url"]) for target in targets}
    expected_target_set = {
        (target["name"], target["url"]) for target in expected_targets
    }
    if actual_target_set != expected_target_set:
        raise PipelineError("crawler result target set does not match shard routing")
    return payload


def certify_shard_completion(
    shard_db: Path,
    result_path: Path,
    region_snapshot: Path,
    expected_region_hash: str,
    shard_index: int,
    shard_total: int,
) -> dict:
    if not shard_db.exists():
        raise PipelineError("crawler exited without creating a shard database")
    metrics = inspect_shard_db(shard_db)
    if metrics is None:
        raise PipelineError("crawler shard database failed structural validation")
    if not result_path.exists():
        raise PipelineError("crawler exited without a result manifest")
    try:
        result_payload = read_json(result_path)
    except (OSError, ValueError) as exc:
        raise PipelineError(f"crawler result manifest is unreadable: {exc}") from exc
    expected_targets = expected_shard_targets(
        region_snapshot, shard_index, shard_total
    )
    validate_crawler_result(
        result_payload, expected_region_hash, expected_targets
    )
    if sha256_file(region_snapshot) != expected_region_hash:
        raise PipelineError("region snapshot changed before shard certification")

    proof = {
        "schema_version": 1,
        "status": "complete",
        "shard_index": shard_index,
        "shard_total": shard_total,
        "db_sha256": sha256_file(shard_db),
        "region_json_sha256": expected_region_hash,
        "result_sha256": sha256_file(result_path),
        "expected_target_count": result_payload["expected_target_count"],
        "failed_target_count": result_payload["failed_target_count"],
        "target_set_sha256": target_set_sha256(expected_targets),
        "certified_at": now_kst().isoformat(),
    }
    write_json(shard_completion_path(shard_db), proof)
    return proof


def inspect_certified_shard(
    shard_db: Path,
    region_snapshot: Path,
    expected_region_hash: str,
    shard_index: int,
    shard_total: int,
) -> dict | None:
    result_path = shard_result_path(shard_db)
    proof_path = shard_completion_path(shard_db)
    if not all(path.exists() for path in (shard_db, result_path, proof_path)):
        return None
    try:
        expected_targets = expected_shard_targets(
            region_snapshot, shard_index, shard_total
        )
        expected_target_hash = target_set_sha256(expected_targets)
        proof = read_json(proof_path)
        if (
            proof.get("schema_version") != 1
            or proof.get("status") != "complete"
            or proof.get("shard_index") != shard_index
            or proof.get("shard_total") != shard_total
            or proof.get("region_json_sha256") != expected_region_hash
            or sha256_file(region_snapshot) != expected_region_hash
            or proof.get("db_sha256") != sha256_file(shard_db)
            or proof.get("result_sha256") != sha256_file(result_path)
            or proof.get("target_set_sha256") != expected_target_hash
        ):
            return None
        result_payload = validate_crawler_result(
            read_json(result_path), expected_region_hash, expected_targets
        )
        if (
            proof.get("expected_target_count")
            != result_payload["expected_target_count"]
            or proof.get("failed_target_count")
            != result_payload["failed_target_count"]
        ):
            return None
        metrics = inspect_shard_db(shard_db)
        if metrics is None:
            return None
        return {**metrics, "completion_proof": str(proof_path)}
    except (OSError, ValueError, PipelineError):
        return None


def run_shard(
    config: LocalConfig, paths: dict[str, Path], shard_index: int
) -> tuple[bool, dict]:
    shard_db = paths["shards"] / f"db_shard_{shard_index}.db"
    result_path = shard_result_path(shard_db)
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
        str(paths.get("region_json", config.region_json_path)),
    ]

    attempts = 0
    max_attempts = config.crawl.retry_failed_shards + 1
    while attempts < max_attempts:
        attempts += 1
        network_ok = wait_for_network_ready(
            ["fin.land.naver.com"],
            paths["logs"] / f"network_shard_{shard_index}.log",
            attempts=config.crawl.network_ready_retry_attempts,
            delay_seconds=config.crawl.network_ready_retry_delay_seconds,
        )
        if not network_ok:
            continue

        archive_incomplete_shard_db(shard_db, f"before_attempt_{attempts}")
        result = run_command(
            command,
            shard_log,
            config.project_root,
            env,
            timeout_seconds=config.crawl.shard_timeout_minutes * 60,
        )
        if result.returncode != 0 and result_path.exists():
            try:
                failure = read_json(result_path)
            except (OSError, ValueError):
                failure = {}
            if failure.get("blocking_error") or failure.get("rate_limit_count", 0):
                raise PipelineError(
                    "crawl blocked; no immediate retry: "
                    + str(failure.get("blocking_error") or "HTTP 429")
                )
        if result.returncode == 0:
            try:
                proof = certify_shard_completion(
                    shard_db,
                    result_path,
                    paths["region_json"],
                    paths["region_json_sha256"],
                    shard_index,
                    config.crawl.shard_total,
                )
                metrics = inspect_shard_db(shard_db)
                if metrics is None:
                    raise PipelineError("certified shard became unreadable")
                return True, {
                    "shard": shard_index,
                    "attempts": attempts,
                    "db_path": str(shard_db),
                    "log_path": str(shard_log),
                    "result_path": str(result_path),
                    "completion_proof": str(shard_completion_path(shard_db)),
                    "expected_target_count": proof["expected_target_count"],
                    **metrics,
                }
            except PipelineError as exc:
                append_log(shard_log, f"SHARD_CERTIFICATION_FAILED: {exc}")

    archive_incomplete_shard_db(shard_db, f"failed_after_{attempts}_attempts")
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


def rebuild_group_results(config: LocalConfig, shard_results: dict[str, dict]) -> list[dict]:
    groups = []
    for group in config.crawl.shard_groups:
        payloads = [
            shard_results[str(shard)]
            for shard in parse_group(group)
            if str(shard) in shard_results
        ]
        if not payloads:
            continue
        groups.append(
            {
                "group": group,
                "completed_shards": sorted(
                    payload["shard"]
                    for payload in payloads
                    if payload.get("status") == "completed"
                ),
                "failed_shards": sorted(
                    payload["shard"]
                    for payload in payloads
                    if payload.get("status") == "failed"
                ),
                "shards": sorted(payloads, key=lambda payload: payload["shard"]),
            }
        )
    return groups


def checkpoint_shard_summary(
    config: LocalConfig,
    paths: dict[str, Path],
    summary: dict,
    shard_results: dict[str, dict],
) -> None:
    summary["shard_results"] = shard_results
    summary["completed_shards"] = sorted(
        int(shard)
        for shard, payload in shard_results.items()
        if payload.get("status") == "completed"
    )
    summary["failed_shards"] = sorted(
        int(shard)
        for shard, payload in shard_results.items()
        if payload.get("status") == "failed"
    )
    summary["groups"] = rebuild_group_results(config, shard_results)
    summary["db_count"] = len(summary["completed_shards"])
    write_json(paths["summary"], summary)


def run_all_shards(
    config: LocalConfig,
    paths: dict[str, Path],
    summary: dict,
    resume: bool = False,
) -> None:
    configured_shards = [
        shard for group in config.crawl.shard_groups for shard in parse_group(group)
    ]
    expected_shards = list(range(config.crawl.shard_total))
    if config.crawl.max_local_parallel <= 0:
        raise PipelineError("max_local_parallel must be positive")
    if sorted(configured_shards) != expected_shards:
        raise PipelineError(
            "shard groups must cover every shard exactly once; "
            f"expected={expected_shards}, configured={configured_shards}"
        )

    shard_results: dict[str, dict] = {}
    if resume:
        for shard_index in range(config.crawl.shard_total):
            shard_db = paths["shards"] / f"db_shard_{shard_index}.db"
            metrics = inspect_certified_shard(
                shard_db,
                paths["region_json"],
                paths["region_json_sha256"],
                shard_index,
                config.crawl.shard_total,
            )
            if metrics is None:
                continue
            shard_results[str(shard_index)] = {
                "shard": shard_index,
                "attempts": 0,
                "db_path": str(shard_db),
                "log_path": str(paths["logs"] / f"shard_{shard_index}.log"),
                "price_count": metrics["price_count"],
                "complex_count": metrics["complex_count"],
                "size_bytes": metrics["size_bytes"],
                "completion_proof": metrics["completion_proof"],
                "reused": True,
                "status": "completed",
            }

    checkpoint_shard_summary(config, paths, summary, shard_results)
    pending_shards = [
        shard for shard in configured_shards if str(shard) not in shard_results
    ]
    for shard in pending_shards:
        archive_incomplete_shard_db(
            paths["shards"] / f"db_shard_{shard}.db", "uncertified_resume"
        )

    # Bound submissions: a blocking failure must not drain a queue of all shards.
    width = config.crawl.max_local_parallel
    with ThreadPoolExecutor(max_workers=width) as executor:
        for offset in range(0, len(pending_shards), width):
            futures = {
                executor.submit(run_shard, config, paths, shard): shard
                for shard in pending_shards[offset:offset + width]
            }
            for future in as_completed(futures):
                shard_index = futures[future]
                try:
                    ok, payload = future.result()
                except Exception as exc:
                    shard_results[str(shard_index)] = {
                        "shard": shard_index, "status": "failed", "error": str(exc),
                    }
                    checkpoint_shard_summary(config, paths, summary, shard_results)
                    for queued in futures:
                        queued.cancel()
                    raise
                payload["status"] = "completed" if ok else "failed"
                payload["reused"] = False
                shard_results[str(shard_index)] = payload
                checkpoint_shard_summary(config, paths, summary, shard_results)


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
    if paths["merged_db"].exists():
        paths["merged_db"].unlink()
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


def validate_merged_db_quality(config: LocalConfig, merged_db: Path) -> dict:
    with sqlite3.connect(merged_db) as conn:
        complex_count = int(
            conn.execute("select count(*) from complexes").fetchone()[0] or 0
        )
        price_count = int(conn.execute("select count(*) from prices").fetchone()[0] or 0)
        priced_complex_count = int(
            conn.execute("select count(distinct complex_no) from prices").fetchone()[0]
            or 0
        )
        date_rows = conn.execute(
            "select date, count(*) from prices group by date order by date"
        ).fetchall()
        region_rows = conn.execute(
            """
            select c.region_depth1, count(*)
            from prices p
            join complexes c on p.complex_no = c.complex_no
            group by c.region_depth1
            order by count(*) desc
            """
        ).fetchall()

    priced_ratio = priced_complex_count / complex_count if complex_count else 0.0
    metrics = {
        "complex_count": complex_count,
        "price_count": price_count,
        "priced_complex_count": priced_complex_count,
        "priced_complex_ratio": priced_ratio,
        "date_rows": date_rows,
        "region_rows": region_rows,
        "min_total_price_records": config.crawl.min_total_price_records,
        "min_priced_complex_ratio": config.crawl.min_priced_complex_ratio,
    }

    failures = []
    if price_count < config.crawl.min_total_price_records:
        failures.append(
            f"price_count {price_count} < {config.crawl.min_total_price_records}"
        )
    if priced_ratio < config.crawl.min_priced_complex_ratio:
        failures.append(
            "priced_complex_ratio "
            f"{priced_ratio:.3f} < {config.crawl.min_priced_complex_ratio:.3f}"
        )

    if failures:
        raise PipelineError(
            "merged DB quality gate failed: "
            + "; ".join(failures)
            + f"; metrics={metrics}"
        )

    return metrics


def run_exports(
    config: LocalConfig, paths: dict[str, Path], date_str: str | None = None
) -> list[str]:
    date_str = date_str or now_kst().strftime("%Y-%m-%d")
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
        if result.returncode != 0:
            raise PipelineError(
                f"export job {export_job.name!r} failed with exit code "
                f"{result.returncode}"
            )
        if not output_path.is_file():
            raise PipelineError(
                f"export job {export_job.name!r} did not create configured output: "
                f"{output_path}"
            )
        generated.append(str(output_path))
    return generated


def upload_files(
    config: LocalConfig,
    paths: dict[str, Path],
    files: list[Path],
    date_prefix: str | None = None,
    previous_uploads: list[dict] | None = None,
    checkpoint=None,
) -> list[dict]:
    uploads = []
    if not config.google_drive.enabled:
        return uploads

    previous_by_file = {
        item.get("file"): item
        for item in (previous_uploads or [])
        if item.get("success")
    }

    for file_path in files:
        fingerprint = {
            "size_bytes": file_path.stat().st_size,
            "sha256": sha256_file(file_path),
        }
        previous = previous_by_file.get(str(file_path))
        if previous and all(
            previous.get(key) == value for key, value in fingerprint.items()
        ):
            uploads.append(
                {
                    **previous,
                    "skipped_duplicate": True,
                    "reason": "same run file was already uploaded",
                }
            )
            if checkpoint:
                checkpoint(uploads)
            continue

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
        if date_prefix:
            command.extend(["--date-prefix", date_prefix])
        result = None
        max_attempts = config.crawl.retry_failed_shards + 1
        for attempt in range(1, max_attempts + 1):
            wait_for_network_ready(
                ["oauth2.googleapis.com", "www.googleapis.com"],
                paths["logs"] / f"network_upload_{file_path.name}.log",
                attempts=config.crawl.network_ready_retry_attempts,
                delay_seconds=config.crawl.network_ready_retry_delay_seconds,
            )
            result = run_command(
                command,
                paths["logs"] / f"upload_{file_path.name}.attempt_{attempt}.log",
                config.project_root,
            )
            if result.returncode == 0:
                break
        uploads.append(
            {
                "file": str(file_path),
                "success": bool(result and result.returncode == 0),
                **fingerprint,
                "date_prefix": date_prefix,
            }
        )
        if checkpoint:
            checkpoint(uploads)
    return uploads


def build_discord_message(summary: dict) -> str:
    run_id = summary["run_id"]
    if summary["status"] == "success":
        return f"✅ [Local Crawl Success] {run_id} completed. DB count={summary['db_count']}, excel files={len(summary['excel_files'])}"
    if summary["status"] == "partial_success":
        failed_uploads = [
            item.get("file")
            for item in summary.get("google_drive_uploads", [])
            if not item.get("success")
        ]
        return (
            f"⚠️ [Local Crawl Partial Success] {run_id} completed with "
            f"failed shards={summary['failed_shards']}, "
            f"failed uploads={len(failed_uploads)}"
        )
    if summary["status"] == "skipped_due_to_lock":
        return (
            f"⏭️ [Local Crawl Skipped] {run_id} skipped because another run is active."
        )
    return f"🚨 [Local Crawl Failure] {run_id} failed at stage={summary['error_stage']}"


def notify_discord(
    config: LocalConfig, paths: dict[str, Path], summary: dict, checkpoint=None
) -> dict | None:
    if not config.discord.enabled:
        return None

    webhook_url = config.discord.webhook_url
    if not webhook_url and config.discord.webhook_file.exists():
        webhook_url = load_webhook_text(config.discord.webhook_file)
    if not webhook_url:
        return {"success": False, "message": "discord webhook missing"}

    message = build_discord_message(summary)
    previous = summary.get("discord_notification")
    if (
        isinstance(previous, dict)
        and previous.get("success")
        and previous.get("message") == message
    ):
        return {
            **previous,
            "skipped_duplicate": True,
            "reason": "same run/status message already sent",
        }
    if (
        isinstance(previous, dict)
        and previous.get("state") == "sending"
        and previous.get("message") == message
    ):
        return {
            **previous,
            "skipped_duplicate": True,
            "reason": "previous Discord send outcome is unknown; suppressing duplicate",
        }

    command = [
        config.python_executable,
        str(config.project_root / "send_discord.py"),
        "--message",
        message,
        "--webhook",
        webhook_url,
    ]
    result = None
    max_attempts = config.crawl.retry_failed_shards + 1
    pending = {"success": False, "state": "sending", "message": message}
    if checkpoint:
        checkpoint(pending)
    for attempt in range(1, max_attempts + 1):
        wait_for_network_ready(
            ["discord.com"],
            paths["logs"] / "network_discord.log",
            attempts=config.crawl.network_ready_retry_attempts,
            delay_seconds=config.crawl.network_ready_retry_delay_seconds,
        )
        result = run_command(
            command,
            paths["logs"] / f"discord.attempt_{attempt}.log",
            config.project_root,
        )
        if result.returncode == 0:
            break
    return {
        "success": bool(result and result.returncode == 0),
        "state": "sent" if result and result.returncode == 0 else "failed",
        "message": message,
    }


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


def find_latest_resumable_run(runtime_root: Path, shard_total: int) -> str | None:
    runs_root = runtime_root / "runs"
    if not runs_root.exists():
        return None
    for run_dir in sorted(
        (path for path in runs_root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    ):
        summary_path = run_dir / "summary.json"
        if not summary_path.exists():
            continue
        try:
            summary = read_json(summary_path)
        except (OSError, ValueError):
            continue
        status = summary.get("status")
        completed = set(summary.get("completed_shards") or [])
        failed = set(summary.get("failed_shards") or [])
        if status == "success":
            return None
        if status == "partial_success":
            return run_dir.name
        if status == "skipped_due_to_lock":
            continue
        if status == "running":
            return run_dir.name
        if status == "failure" and (failed or len(completed) < shard_total):
            return run_dir.name
        if status == "failure":
            return None
    return None


def finalize_summary(summary: dict, path: Path) -> None:
    summary["finished_at"] = now_kst().isoformat()
    write_json(path, summary)


def run_pipeline(config: LocalConfig, run_id: str, resume: bool = False) -> int:
    paths = build_runtime_paths(config, run_id, create=False)

    try:
        with file_lock(
            paths["locks"] / "daily_crawl.lock", run_id, "local.orchestrator"
        ):
            if resume:
                if not paths["summary"].exists():
                    raise PipelineError(
                        f"resume summary not found for run_id={run_id}"
                    )
                try:
                    summary = read_json(paths["summary"])
                except (OSError, ValueError) as exc:
                    raise PipelineError(
                        f"resume summary is unreadable for run_id={run_id}: {exc}"
                    ) from exc
                if summary.get("status") == "success":
                    raise PipelineError(
                        f"run_id={run_id} is already complete with "
                        f"status={summary.get('status')}"
                    )
            else:
                if paths["run_dir"].exists():
                    raise PipelineError(
                        f"run directory already exists for run_id={run_id}; "
                        "use explicit resume"
                    )
                summary = default_summary(run_id, paths["run_dir"])

            paths = build_runtime_paths(config, run_id)
            if not resume:
                write_json(paths["summary"], summary)

            latest_link = paths["runtime_root"] / "latest"

            def checkpoint_discord(payload: dict) -> None:
                summary["discord_notification"] = payload
                write_json(paths["summary"], summary)

            try:
                preflight(config, paths)

                if resume:
                    summary["status"] = "running"
                    summary["finished_at"] = None
                    summary["error_stage"] = None
                    summary["resume_count"] = int(
                        summary.get("resume_count") or 0
                    ) + 1
                    summary["last_resumed_at"] = now_kst().isoformat()

                prepare_run_inputs(config, paths, summary, resume)
                summary["stage"] = "crawling"
                write_json(paths["summary"], summary)
                run_all_shards(config, paths, summary, resume=resume)

                summary["db_count"] = len(summary["completed_shards"])
                write_failed_shards(
                    paths["failed_shards"], summary["failed_shards"]
                )

                if summary["failed_shards"]:
                    raise PipelineError(
                        "shard crawl failed; refusing merge/export/upload: "
                        + ", ".join(
                            str(value) for value in summary["failed_shards"]
                        )
                    )
                expected_shards = list(range(config.crawl.shard_total))
                if summary["completed_shards"] != expected_shards:
                    raise PipelineError(
                        "certified shard set is incomplete; refusing "
                        "merge/export/upload"
                    )

                summary["stage"] = "merging"
                write_json(paths["summary"], summary)
                summary["merged_db_path"] = str(run_merge(config, paths))
                summary["merged_db_quality"] = validate_merged_db_quality(
                    config, paths["merged_db"]
                )
                write_json(paths["summary"], summary)

                summary["stage"] = "exporting"
                write_json(paths["summary"], summary)
                run_date = str(summary["started_at"]).split("T", 1)[0]
                summary["excel_files"] = run_exports(
                    config, paths, date_str=run_date
                )
                write_json(paths["summary"], summary)

                upload_targets = [paths["merged_db"]] + [
                    Path(path) for path in summary["excel_files"]
                ]
                previous_uploads = list(
                    summary.get("google_drive_uploads") or []
                )
                summary["stage"] = "uploading"
                write_json(paths["summary"], summary)

                def checkpoint_uploads(uploads: list[dict]) -> None:
                    summary["google_drive_uploads"] = list(uploads)
                    write_json(paths["summary"], summary)

                summary["google_drive_uploads"] = upload_files(
                    config,
                    paths,
                    upload_targets,
                    date_prefix=run_date,
                    previous_uploads=previous_uploads,
                    checkpoint=checkpoint_uploads,
                )
                write_json(paths["summary"], summary)

                upload_failures = [
                    upload
                    for upload in summary["google_drive_uploads"]
                    if not upload.get("success")
                ]

                if upload_failures:
                    summary["status"] = "partial_success"
                    summary["stage"] = "delivery_failed"
                else:
                    summary["status"] = "success"
                    summary["stage"] = "delivery_complete"
                write_json(paths["summary"], summary)
                summary["discord_notification"] = notify_discord(
                    config, paths, summary, checkpoint=checkpoint_discord
                )
                write_json(paths["summary"], summary)

            except PipelineError as exc:
                summary["status"] = "failure"
                summary["error_stage"] = str(exc)
                summary["stage"] = "failed"
                summary["discord_notification"] = notify_discord(
                    config, paths, summary, checkpoint=checkpoint_discord
                )
                finalize_summary(summary, paths["summary"])
                return 1
            except Exception as exc:
                summary["status"] = "failure"
                summary["error_stage"] = repr(exc)
                summary["stage"] = "failed"
                summary["discord_notification"] = notify_discord(
                    config, paths, summary, checkpoint=checkpoint_discord
                )
                finalize_summary(summary, paths["summary"])
                return 1

            if summary["status"] == "partial_success":
                cleanup_old_runs(config, paths)
                finalize_summary(summary, paths["summary"])
                return 1

            if latest_link.exists() or latest_link.is_symlink():
                latest_link.unlink()
            latest_link.symlink_to(
                paths["run_dir"], target_is_directory=True
            )

            cleanup_old_runs(config, paths)
            summary["stage"] = "complete"
            finalize_summary(summary, paths["summary"])
            return 0

    except LockError:
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local crawl pipeline on Mac mini")
    parser.add_argument(
        "--config", default="local/config.json", help="Path to local config file"
    )
    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument("--run-id", help="Optional new run id override")
    run_mode.add_argument("--resume-run", help="Resume this existing run id")
    run_mode.add_argument(
        "--resume-latest",
        action="store_true",
        help="Resume the newest interrupted/incomplete run, or exit when none exists",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    if args.resume_latest:
        run_id = find_latest_resumable_run(
            config.runtime_root, config.crawl.shard_total
        )
        if not run_id:
            print("No resumable crawl run found; recovery exits without starting a new run.")
            return 0
        print(f"Resuming latest crawl run: {run_id}")
        return run_pipeline(config, run_id, resume=True)
    if args.resume_run:
        if Path(args.resume_run).name != args.resume_run:
            raise SystemExit("--resume-run must be a run id, not a path")
        return run_pipeline(config, args.resume_run, resume=True)
    run_id = args.run_id or make_run_id()
    return run_pipeline(config, run_id, resume=False)


if __name__ == "__main__":
    raise SystemExit(main())
