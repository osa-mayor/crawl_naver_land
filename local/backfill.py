from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from local.lock import LockError, file_lock
from merge_db import PRICE_IDENTITY_COLUMNS, merge_databases


SCHEMA_VERSION = 1
DEFAULT_SOURCE_RUN = "runtime/runs/20260921_010003_kst"


class BackfillError(RuntimeError):
    pass


def now_kst() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def valid_leaf_targets(region_payload: object) -> list[dict[str, str]]:
    """Return complex-bearing leaf regions, never administrative/root nodes."""
    if not isinstance(region_payload, dict):
        raise BackfillError("region snapshot must be a JSON object")

    targets: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    def walk(node: object, path: list[str]) -> None:
        if not isinstance(node, dict):
            raise BackfillError(f"invalid region node: {' '.join(path)}")
        children = node.get("children")
        if isinstance(children, dict) and children:
            # A stale false marker on an administrative parent must not hide
            # a validated true descendant.
            for child_name, child in children.items():
                walk(child, [*path, str(child_name)])
            return
        if len(path) < 2 or node.get("has_complexes") is not True:
            return
        url = node.get("url")
        if not isinstance(url, str) or not url.strip():
            raise BackfillError(f"complex-bearing leaf has no URL: {' '.join(path)}")
        if url in seen_urls:
            raise BackfillError(f"duplicate leaf URL in region snapshot: {url}")
        seen_urls.add(url)
        targets.append({"name": " ".join(path), "url": url})

    for root_name, root in region_payload.items():
        walk(root, [str(root_name)])
    targets.sort(key=lambda row: (row["name"], row["url"]))
    if not targets:
        raise BackfillError("region snapshot contains no valid complex-bearing leaves")
    return targets


def validate_target_override(payload: object) -> list[dict[str, str]]:
    if not isinstance(payload, list) or not payload:
        raise BackfillError("--targets-json must contain a non-empty list")
    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise BackfillError("each override target must be an object")
        name, url = item.get("name"), item.get("url")
        if not isinstance(name, str) or not name.strip():
            raise BackfillError("override target name is missing")
        if not isinstance(url, str) or not url.strip():
            raise BackfillError("override target URL is missing")
        if url in seen:
            raise BackfillError(f"duplicate override target URL: {url}")
        seen.add(url)
        targets.append({"name": name.strip(), "url": url.strip()})
    return targets


def batch_targets(
    targets: list[dict[str, str]], batch_size: int
) -> list[list[dict[str, str]]]:
    if batch_size <= 0:
        raise BackfillError("--batch-size must be positive")
    return [targets[index : index + batch_size] for index in range(0, len(targets), batch_size)]


def inspect_db(path: Path) -> dict[str, int] | None:
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            check = connection.execute("pragma quick_check").fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
            if not check or check[0] != "ok" or not {"complexes", "prices"} <= tables:
                return None
            return {
                "complex_count": int(
                    connection.execute("select count(*) from complexes").fetchone()[0]
                ),
                "price_count": int(
                    connection.execute("select count(*) from prices").fetchone()[0]
                ),
            }
    except (OSError, sqlite3.Error):
        return None


def validate_crawler_result(
    payload: object,
    expected_targets: list[dict[str, str]],
    expected_region_hash: str,
) -> dict:
    if not isinstance(payload, dict):
        raise BackfillError("crawler result is not a JSON object")
    if payload.get("schema_version") != 1 or payload.get("status") != "complete":
        raise BackfillError("crawler result is not a schema-1 complete result")
    if payload.get("mode") != "backfill" or payload.get("saved") is not True:
        raise BackfillError("crawler result is not a saved backfill result")
    if payload.get("region_json_sha256") != expected_region_hash:
        raise BackfillError("crawler result region hash mismatch")
    expected_count = payload.get("expected_target_count")
    failed_count = payload.get("failed_target_count")
    if isinstance(expected_count, bool) or expected_count != len(expected_targets):
        raise BackfillError("crawler result expected target count mismatch")
    if isinstance(failed_count, bool) or failed_count != 0:
        raise BackfillError("crawler result contains failed targets")
    actual = payload.get("targets")
    if not isinstance(actual, list) or len(actual) != len(expected_targets):
        raise BackfillError("crawler result target count mismatch")

    expected_by_url = {target["url"]: target["name"] for target in expected_targets}
    actual_urls: set[str] = set()
    for target in actual:
        if not isinstance(target, dict):
            raise BackfillError("crawler result target is not an object")
        name, url, status = target.get("name"), target.get("url"), target.get("status")
        if url in actual_urls or expected_by_url.get(url) != name:
            raise BackfillError("crawler result target identity mismatch")
        actual_urls.add(url)
        if status not in {"complete", "empty"}:
            raise BackfillError(f"crawler target did not complete: {name}")
        total, ids, apartment_ids = (
            target.get("total"),
            target.get("ids"),
            target.get("apartment_ids"),
        )
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise BackfillError(f"crawler target total is invalid: {name}")
        if not isinstance(ids, list) or not isinstance(apartment_ids, list):
            raise BackfillError(f"crawler target inventories are missing: {name}")
        if any(isinstance(value, bool) or not isinstance(value, (str, int)) for value in [*ids, *apartment_ids]):
            raise BackfillError(f"crawler target inventory IDs are invalid: {name}")
        id_keys = {(type(value).__name__, str(value)) for value in ids}
        apartment_keys = {
            (type(value).__name__, str(value)) for value in apartment_ids
        }
        if len(id_keys) != len(ids) or len(apartment_keys) != len(apartment_ids):
            raise BackfillError(f"crawler target inventory IDs are duplicated: {name}")
        if not apartment_keys <= id_keys:
            raise BackfillError(f"crawler apartment IDs are not a subset: {name}")
        if total != len(ids) or len(apartment_ids) > total:
            raise BackfillError(f"crawler target inventory counts are inconsistent: {name}")
        if status == "empty" and apartment_ids:
            raise BackfillError(f"empty crawler target contains apartments: {name}")
    if actual_urls != set(expected_by_url):
        raise BackfillError("crawler result omitted expected targets")
    return payload


def binding_for_batch(
    *,
    batch_index: int,
    target_path: Path,
    source_db: Path,
    region_snapshot: Path,
    code_paths: dict[str, Path],
) -> dict:
    return {
        "batch_index": batch_index,
        "target_sha256": sha256_file(target_path),
        "source_db_sha256": sha256_file(source_db),
        "region_json_sha256": sha256_file(region_snapshot),
        "code_sha256": {name: sha256_file(path) for name, path in code_paths.items()},
    }


def binding_for_generation(
    *,
    batch_index: int,
    target_path: Path,
    manifest: dict,
    code_sha256: dict[str, str],
) -> dict:
    return {
        "batch_index": batch_index,
        "target_sha256": sha256_file(target_path),
        "source_db_sha256": manifest["source_db_sha256"],
        "region_json_sha256": manifest["region_json_sha256"],
        "code_sha256": code_sha256,
    }


def pin_run_inputs(path: Path, current: dict) -> dict:
    if path.exists():
        if read_json(path) != current:
            raise BackfillError('run inputs changed; refusing mixed-source/code resume')
    else:
        atomic_json(path, current)
    return current


def _manifest_code_generations(manifest: dict) -> list[dict[str, str]]:
    generations = manifest.get("code_generations")
    if generations is None:
        code = manifest.get("code_sha256")
        return [code] if isinstance(code, dict) else []
    if not isinstance(generations, list) or not all(
        isinstance(item, dict) and isinstance(item.get("code_sha256"), dict)
        for item in generations
    ):
        raise BackfillError("run manifest code generation history is invalid")
    return [item["code_sha256"] for item in generations]


def prepare_run_manifest(
    path: Path,
    current: dict,
    *,
    accept_code_update: bool,
    validate_prior_batches,
) -> dict:
    """Pin immutable inputs and explicitly audit a code-only generation change."""
    if not path.exists():
        atomic_json(path, current)
        return current

    prior = read_json(path)
    if not isinstance(prior, dict):
        raise BackfillError("run manifest is not a JSON object")
    immutable_keys = (
        "source_db_sha256",
        "region_json_sha256",
        "targets_sha256",
        "config_sha256",
        "batch_size",
    )
    changed = [key for key in immutable_keys if prior.get(key) != current.get(key)]
    if changed:
        raise BackfillError(
            "run inputs changed; refusing resume (" + ", ".join(changed) + ")"
        )
    if prior.get("code_sha256") == current.get("code_sha256"):
        return prior
    if not accept_code_update:
        raise BackfillError(
            "run code changed; ordinary resume refused (use --accept-code-update "
            "to audit and accept a new generation)"
        )

    prior_generations = _manifest_code_generations(prior)
    if not prior_generations or prior_generations[-1] != prior.get("code_sha256"):
        raise BackfillError("run manifest current code generation is invalid")
    validated_batches = validate_prior_batches(prior_generations)
    accepted_at = now_kst().isoformat()
    generations = prior.get("code_generations")
    if generations is None:
        generations = [
            {
                "generation": 0,
                "code_sha256": prior["code_sha256"],
                "accepted_at": None,
            }
        ]
    else:
        generations = list(generations)
    upgraded = {
        **prior,
        "code_sha256": current["code_sha256"],
        "code_generations": [
            *generations,
            {
                "generation": len(generations),
                "code_sha256": current["code_sha256"],
                "accepted_at": accepted_at,
            },
        ],
        "code_transitions": [
            *prior.get("code_transitions", []),
            {
                "accepted_at": accepted_at,
                "accepted_old_code_sha256": prior["code_sha256"],
                "new_code_sha256": current["code_sha256"],
                "validated_certified_batches": validated_batches,
            },
        ],
    }
    atomic_json(path, upgraded)
    return upgraded


def verify_run_inputs(pinned: dict, source_db: Path, snapshot: Path, code_paths: dict) -> None:
    current = {
        'source_db_sha256': sha256_file(source_db),
        'region_json_sha256': sha256_file(snapshot),
        'code_sha256': {name: sha256_file(path) for name, path in code_paths.items()},
    }
    if any(pinned.get(key) != value for key, value in current.items()):
        raise BackfillError('run inputs changed during backfill; refusing certification/merge')


def certify_batch(
    db_path: Path,
    result_path: Path,
    proof_path: Path,
    targets: list[dict[str, str]],
    binding: dict,
) -> dict:
    metrics = inspect_db(db_path)
    if metrics is None:
        raise BackfillError("batch database failed integrity/schema validation")
    if not result_path.is_file():
        raise BackfillError("crawler did not write a result manifest")
    result = validate_crawler_result(
        read_json(result_path), targets, binding["region_json_sha256"]
    )
    proof = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        **binding,
        "db_sha256": sha256_file(db_path),
        "result_sha256": sha256_file(result_path),
        "expected_target_count": result["expected_target_count"],
        "failed_target_count": result["failed_target_count"],
        "metrics": metrics,
        "certified_at": now_kst().isoformat(),
    }
    atomic_json(proof_path, proof)
    return proof


def inspect_certified_batch(
    db_path: Path,
    result_path: Path,
    proof_path: Path,
    targets: list[dict[str, str]],
    binding: dict,
) -> dict | None:
    if not all(path.is_file() for path in (db_path, result_path, proof_path)):
        return None
    try:
        proof = read_json(proof_path)
        if not isinstance(proof, dict):
            return None
        for key, value in binding.items():
            if proof.get(key) != value:
                return None
        if (
            proof.get("schema_version") != SCHEMA_VERSION
            or proof.get("status") != "complete"
            or proof.get("db_sha256") != sha256_file(db_path)
            or proof.get("result_sha256") != sha256_file(result_path)
            or inspect_db(db_path) is None
        ):
            return None
        result = validate_crawler_result(
            read_json(result_path), targets, binding["region_json_sha256"]
        )
        if proof.get("expected_target_count") != result["expected_target_count"]:
            return None
        return proof
    except (OSError, ValueError, BackfillError):
        return None


def archive_uncertified(paths: Iterable[Path], archive_root: Path, batch_index: int) -> None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return
    archive = archive_root / f"batch_{batch_index:04d}_{now_kst().strftime('%H%M%S_%f')}"
    archive.mkdir(parents=True)
    for path in existing:
        path.replace(archive / path.name)


def run_logged(command: list[str], log_path: Path, cwd: Path, env: dict | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write("COMMAND: " + " ".join(command) + "\n")
        log.flush()
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log.write(f"RETURN_CODE: {completed.returncode}\n")
    return completed.returncode


def log_indicates_rate_limit(*paths: Path) -> bool:
    markers = ("429", "rate limit", "rate-limit", "rate limiting")
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if any(marker in text for marker in markers):
            return True
    return False


def remove_source_price_conflicts(batch_copy: Path, source_db: Path) -> int:
    """Keep every original source price row authoritative during merge."""
    quoted = [f'"{column}"' for column in PRICE_IDENTITY_COLUMNS]
    predicates = " and ".join(
        f"b.{column} is s.{column}" for column in quoted
    )
    with sqlite3.connect(batch_copy) as connection:
        connection.execute("attach database ? as source", (str(source_db),))
        try:
            cursor = connection.execute(
                "delete from main.prices as b where exists "
                f"(select 1 from source.prices as s where {predicates})"
            )
            removed = max(cursor.rowcount, 0)
            connection.commit()
        finally:
            connection.execute("detach database source")
    return removed


def merge_from_copy(source_db: Path, batch_dbs: list[Path], output_db: Path) -> dict:
    if not batch_dbs:
        raise BackfillError("no certified batch databases to merge")
    temporary = output_db.with_name(f".{output_db.name}.{os.getpid()}.tmp")
    merge_inputs = output_db.parent / "merge_inputs"
    if temporary.exists():
        temporary.unlink()
    if merge_inputs.exists():
        shutil.rmtree(merge_inputs)
    merge_inputs.mkdir(parents=True)
    shutil.copy2(source_db, temporary)
    removed_conflicts = 0
    try:
        for index, batch_db in enumerate(batch_dbs):
            copied = merge_inputs / f"batch_{index:04d}.db"
            shutil.copy2(batch_db, copied)
            removed_conflicts += remove_source_price_conflicts(copied, source_db)
        merge_databases(str(temporary), str(merge_inputs / "batch_*.db"))
        if inspect_db(temporary) is None:
            raise BackfillError("merged database failed validation")
        os.replace(temporary, output_db)
    finally:
        if temporary.exists():
            temporary.unlink()
        shutil.rmtree(merge_inputs, ignore_errors=True)
    return {"removed_source_price_conflicts": removed_conflicts, **(inspect_db(output_db) or {})}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe resumable missing-data backfill")
    parser.add_argument("--source-run", default=DEFAULT_SOURCE_RUN)
    parser.add_argument("--config", default="local/config.json")
    parser.add_argument("--output-root")
    parser.add_argument("--date", help="Output date YYYYMMDD (defaults to KST today)")
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--targets-json", type=Path)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--export", action="store_true", dest="run_exports")
    parser.add_argument("--rate-limit-cooldown-minutes", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--accept-code-update",
        action="store_true",
        help="audit certified batches and accept a code-only resume generation",
    )
    return parser


def _load_config(path: Path) -> dict:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise BackfillError("config must be a JSON object")
    return payload


def _source_paths(source_run: Path) -> tuple[Path, Path]:
    summary_path = source_run / "summary.json"
    if not summary_path.is_file():
        raise BackfillError(f"source summary is missing: {summary_path}")
    summary = read_json(summary_path)
    if not isinstance(summary, dict) or summary.get("status") != "success":
        raise BackfillError("source run is not a successful completed run")
    source_db = source_run / "merged.db"
    region = source_run / "inputs" / "naver_region_codes.json"
    if not source_db.is_file() or inspect_db(source_db) is None:
        raise BackfillError("source merged database is missing or invalid")
    if not region.is_file():
        raise BackfillError("source region snapshot is missing")
    return source_db, region


def _status_base(
    source_run: Path,
    source_db: Path,
    snapshot: Path,
    targets: list[dict],
    selection_scope: str,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_run": str(source_run),
        "source_db": str(source_db),
        "source_db_sha256": sha256_file(source_db),
        "region_json_sha256": sha256_file(snapshot),
        "expected_target_count": len(targets),
        "selection_scope": selection_scope,
        "nation_complete": False,
        "completed_batches": [],
        "failed_batch": None,
        "merged_db": None,
        "exports": [],
    }


def _export_jobs(
    config: dict, project_root: Path, python: str, output_dir: Path, merged_db: Path
) -> list[str]:
    generated: list[str] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    date_text = now_kst().strftime("%Y-%m-%d")
    for job in config.get("exports", []):
        output = output_dir / f"export_{date_text}_{job['name']}.xlsx"
        command = [
            python,
            str(project_root / "export_db.py"),
            str(merged_db),
            "--output",
            str(output),
            "--min-households",
            str(int(job.get("min_households", 0))),
            "--region",
            *[str(region) for region in job.get("regions", [])],
        ]
        if run_logged(command, output_dir.parent / "logs" / f"export_{job['name']}.log", project_root) != 0:
            raise BackfillError(f"export failed: {job['name']}")
        if not output.is_file():
            raise BackfillError(f"export did not create output: {job['name']}")
        generated.append(str(output))
    return generated


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = (project_root / config_path).resolve()
    config = _load_config(config_path)
    configured_root = Path(config.get("project_root", project_root)).expanduser()
    if not configured_root.is_absolute():
        configured_root = (config_path.parent / configured_root).resolve()
    project_root = configured_root

    source_run = Path(args.source_run).expanduser()
    if not source_run.is_absolute():
        source_run = (project_root / source_run).resolve()
    source_db, source_region = _source_paths(source_run)

    date_value = args.date or now_kst().strftime("%Y%m%d")
    if not re.fullmatch(r"\d{8}", date_value):
        raise BackfillError("--date must be YYYYMMDD")
    runtime_root = Path(config.get("runtime_root", "runtime"))
    if not runtime_root.is_absolute():
        runtime_root = project_root / runtime_root
    output_root = Path(args.output_root).expanduser() if args.output_root else runtime_root / "backfills"
    if not output_root.is_absolute():
        output_root = project_root / output_root
    run_dir = output_root / f"{source_run.name}_{date_value}"
    lock_path = runtime_root / "locks" / "daily_crawl.lock"
    run_id = f"backfill_{source_run.name}_{date_value}"
    try:
        with file_lock(lock_path, run_id, "local.backfill"):
            return _run_locked(
                args=args,
                project_root=project_root,
                config=config,
                source_run=source_run,
                source_db=source_db,
                source_region=source_region,
                run_dir=run_dir,
            )
    except LockError as exc:
        raise BackfillError(str(exc)) from exc


def _run_locked(
    *,
    args: argparse.Namespace,
    project_root: Path,
    config: dict,
    source_run: Path,
    source_db: Path,
    source_region: Path,
    run_dir: Path,
) -> int:
    snapshot = run_dir / "inputs" / "naver_region_codes.json"
    if snapshot.exists():
        if sha256_file(snapshot) != sha256_file(source_region):
            raise BackfillError("existing backfill snapshot differs from source run snapshot")
    else:
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_region, snapshot)

    all_targets = valid_leaf_targets(read_json(snapshot))
    targets = (
        validate_target_override(read_json(args.targets_json.resolve()))
        if args.targets_json
        else all_targets
    )
    if args.limit is not None:
        if args.limit <= 0:
            raise BackfillError("--limit must be positive")
        targets = targets[: args.limit]
    selection_scope = (
        "explicit" if args.targets_json else "limited" if args.limit is not None else "nation"
    )
    operation_dir = run_dir
    if selection_scope != "nation":
        operation_dir = run_dir / "scopes" / f"{selection_scope}_{sha256_json(targets)[:12]}"
    inputs_dir, batches_dir, logs_dir = (
        operation_dir / "inputs",
        operation_dir / "batches",
        operation_dir / "logs",
    )
    batches = batch_targets(targets, args.batch_size)
    status_path = operation_dir / "status.json"
    previous_status = read_json(status_path) if status_path.exists() else None
    if isinstance(previous_status, dict) and previous_status.get("status") == "cooldown":
        retry_after = previous_status.get("retry_after")
        try:
            if retry_after and now_kst() < datetime.fromisoformat(str(retry_after)):
                raise BackfillError(f"global rate-limit cooldown active until {retry_after}")
        except ValueError as exc:
            raise BackfillError("saved cooldown timestamp is invalid") from exc
    status = _status_base(
        source_run, source_db, snapshot, targets, selection_scope
    )
    status.update({"status": "planned" if args.dry_run else "running", "batch_count": len(batches)})
    if args.dry_run:
        print(json.dumps({"run_dir": str(operation_dir), "targets": len(targets), "batches": len(batches), "selection_scope": selection_scope}, ensure_ascii=False))
        return 0
    batches_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    code_paths = {
        "backfill": Path(__file__).resolve(),
        "crawler": project_root / "crawler.py",
        "selectors": project_root / "land_selectors.py",
        "merge_db": project_root / "merge_db.py",
        "export": project_root / "export_db.py",
    }
    if not all(path.is_file() for path in code_paths.values()):
        raise BackfillError("required backfill code file is missing")
    current_manifest = {
        'source_db_sha256': sha256_file(source_db),
        'region_json_sha256': sha256_file(snapshot),
        'code_sha256': {name: sha256_file(path) for name, path in code_paths.items()},
        'targets_sha256': sha256_json(targets),
        'config_sha256': sha256_json(config),
        'batch_size': args.batch_size,
    }
    for index, batch in enumerate(batches):
        target_path = inputs_dir / f"batch_{index:04d}.targets.json"
        if target_path.exists():
            if read_json(target_path) != batch:
                raise BackfillError(f"existing target file changed: {target_path}")
        else:
            atomic_json(target_path, batch)

    def validate_prior_batches(prior_generations: list[dict[str, str]]) -> list[int]:
        validated: list[int] = []
        for proof_path in sorted(batches_dir.glob("batch_*.db.complete.json")):
            match = re.fullmatch(r"batch_(\d{4})\.db\.complete\.json", proof_path.name)
            if match is None:
                raise BackfillError(f"unexpected certified proof path: {proof_path}")
            index = int(match.group(1))
            if index >= len(batches):
                raise BackfillError(f"certified proof is outside current target batches: {proof_path}")
            stem = f"batch_{index:04d}"
            target_path = inputs_dir / f"{stem}.targets.json"
            db_path = batches_dir / f"{stem}.db"
            result_path = Path(str(db_path) + ".result.json")
            valid = any(
                inspect_certified_batch(
                    db_path,
                    result_path,
                    proof_path,
                    batches[index],
                    binding_for_generation(
                        batch_index=index,
                        target_path=target_path,
                        manifest=current_manifest,
                        code_sha256=code_sha256,
                    ),
                )
                is not None
                for code_sha256 in prior_generations
            )
            if not valid:
                raise BackfillError(
                    f"certified batch proof failed prior-generation audit: {proof_path}"
                )
            validated.append(index)
        return validated

    pinned = prepare_run_manifest(
        inputs_dir / "run_manifest.json",
        current_manifest,
        accept_code_update=args.accept_code_update,
        validate_prior_batches=validate_prior_batches,
    )
    generation_hashes = _manifest_code_generations(pinned)
    atomic_json(status_path, status)

    def verify_pinned():
        try:
            verify_run_inputs(pinned, source_db, snapshot, code_paths)
        except BackfillError:
            status.update(status='failed', error='run input drift')
            atomic_json(status_path, status)
            raise

    python = str(config.get("python_executable") or sys.executable)
    crawl = config.get("crawl", {})
    env = os.environ.copy()
    for key, config_key, default in (
        ("MAX_CONCURRENT_PAGES", "max_concurrent_pages", 2),
        ("MAX_API_PREFETCH_CONCURRENCY", "max_api_prefetch_concurrency", 2),
        ("NAVIGATION_RETRY_ATTEMPTS", "navigation_retry_attempts", 3),
        ("MAX_RATE_LIMIT_RESPONSES", "max_rate_limit_responses", 20),
        ("ARTICLE_RESPONSE_TIMEOUT_MS", "article_response_timeout_ms", 15000),
        ("MAX_ARTICLE_PAGES_PER_TRADE", "max_article_pages_per_trade", 80),
    ):
        env[key] = str(crawl.get(config_key, default))
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"})

    certified_dbs: list[Path] = []
    for index, batch in enumerate(batches):
        verify_pinned()
        stem = f"batch_{index:04d}"
        target_path = inputs_dir / f"{stem}.targets.json"
        db_path = batches_dir / f"{stem}.db"
        result_path = Path(str(db_path) + ".result.json")
        proof_path = Path(str(db_path) + ".complete.json")
        binding = binding_for_batch(
            batch_index=index,
            target_path=target_path,
            source_db=source_db,
            region_snapshot=snapshot,
            code_paths=code_paths,
        )
        proof = None
        for code_sha256 in generation_hashes:
            generation_binding = binding_for_generation(
                batch_index=index,
                target_path=target_path,
                manifest=pinned,
                code_sha256=code_sha256,
            )
            proof = inspect_certified_batch(
                db_path, result_path, proof_path, batch, generation_binding
            )
            if proof is not None:
                break
        if proof is None:
            archive_uncertified(
                (db_path, result_path, proof_path), batches_dir / "archive", index
            )
            command = [
                python,
                str(project_root / "crawler.py"),
                "--targets-json",
                str(target_path),
                "--skip-existing-db",
                str(source_db),
                "--region-json-path",
                str(snapshot),
                "--db-path",
                str(db_path),
                "--log-path",
                str(logs_dir / f"crawler_{stem}.log"),
                "--screenshot-dir",
                str(operation_dir / "screenshots"),
            ]
            if args.headed:
                command.append("--headed")
            return_code = run_logged(command, logs_dir / f"{stem}.log", project_root, env)
            verify_pinned()
            if return_code != 0:
                rate_limited = log_indicates_rate_limit(
                    logs_dir / f"{stem}.log",
                    logs_dir / f"crawler_{stem}.log",
                )
                status.update(
                    {
                        "status": "cooldown" if rate_limited else "failed",
                        "failed_batch": index,
                        "retry_after": (
                            now_kst()
                            + timedelta(minutes=args.rate_limit_cooldown_minutes)
                        ).isoformat()
                        if rate_limited
                        else None,
                    }
                )
                atomic_json(status_path, status)
                raise BackfillError(
                    f"batch {index} failed (including rate-limit stops); rerun to resume"
                )
            try:
                proof = certify_batch(
                    db_path, result_path, proof_path, batch, binding
                )
            except BackfillError:
                rate_limited = log_indicates_rate_limit(
                    logs_dir / f"{stem}.log",
                    logs_dir / f"crawler_{stem}.log",
                    result_path,
                )
                status.update(
                    {
                        "status": "cooldown" if rate_limited else "failed",
                        "failed_batch": index,
                        "retry_after": (
                            now_kst()
                            + timedelta(minutes=args.rate_limit_cooldown_minutes)
                        ).isoformat()
                        if rate_limited
                        else None,
                    }
                )
                atomic_json(status_path, status)
                raise
        certified_dbs.append(db_path)
        status["completed_batches"] = list(range(index + 1))
        status["last_certification"] = str(proof_path)
        atomic_json(status_path, status)

    verify_pinned()
    output_db = operation_dir / "merged.db"
    merge_metrics = merge_from_copy(source_db, certified_dbs, output_db)
    verify_pinned()
    status.update(
        {
            "status": "complete",
            "nation_complete": selection_scope == "nation",
            "failed_batch": None,
            "merged_db": str(output_db),
            "merged_db_sha256": sha256_file(output_db),
            "merge_metrics": merge_metrics,
        }
    )
    if args.run_exports:
        status["exports"] = _export_jobs(
            config, project_root, python, operation_dir / "exports", output_db
        )
    status["finished_at"] = now_kst().isoformat()
    verify_pinned()
    atomic_json(status_path, status)
    print(json.dumps({"status": "complete", "run_dir": str(operation_dir), "selection_scope": selection_scope, "nation_complete": status["nation_complete"], "merged_db": status["merged_db"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackfillError as exc:
        print(f"BACKFILL_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
