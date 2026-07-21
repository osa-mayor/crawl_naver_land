from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SHARD_GROUPS = [
    "0-1",
    "2-3",
    "4-5",
    "6-7",
    "8-9",
    "10-11",
    "12-13",
    "14-15",
    "16-17",
    "18-19",
]

DEFAULT_EXPORTS = [
    {
        "name": "Seoul",
        "regions": ["서울시"],
        "min_households": 100,
    },
    {
        "name": "Gyeonggi",
        "regions": ["경기도"],
        "min_households": 100,
    },
    {
        "name": "Metros",
        "regions": [
            "인천시",
            "부산시",
            "대구시",
            "광주시",
            "대전시",
            "울산시",
            "세종시",
        ],
        "min_households": 100,
    },
    {
        "name": "Provinces",
        "regions": [
            "강원도",
            "충청북도",
            "충청남도",
            "경상북도",
            "경상남도",
            "전북도",
            "전라남도",
            "제주도",
        ],
        "min_households": 100,
    },
]


@dataclass(frozen=True)
class CrawlSettings:
    shard_total: int
    shard_groups: list[str]
    max_local_parallel: int
    max_concurrent_pages: int
    max_api_prefetch_concurrency: int
    retry_failed_shards: int
    shard_timeout_minutes: int
    navigation_retry_attempts: int
    network_ready_retry_attempts: int
    network_ready_retry_delay_seconds: float
    max_failed_region_ratio: float


@dataclass(frozen=True)
class ExportJob:
    name: str
    regions: list[str]
    min_households: int


@dataclass(frozen=True)
class GoogleDriveSettings:
    enabled: bool
    folder_id: str
    token_file: Path


@dataclass(frozen=True)
class DiscordSettings:
    enabled: bool
    webhook_url: str
    webhook_file: Path


@dataclass(frozen=True)
class ValidatorSettings:
    enabled: bool
    runtime_root: Path
    repo_root: Path
    region_file: Path
    max_concurrent_tabs: int
    timeout_ms: int
    expected_branch: str
    remote_name: str
    commit_name: str
    commit_email: str
    commit_message_template: str
    push_changes: bool


@dataclass(frozen=True)
class LocalConfig:
    project_root: Path
    runtime_root: Path
    region_json_path: Path
    python_executable: str
    preflight_smoke_test: bool
    retention_days: int
    crawl: CrawlSettings
    export_jobs: list[ExportJob]
    google_drive: GoogleDriveSettings
    discord: DiscordSettings
    validator: ValidatorSettings


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def load_config(path: str | Path) -> LocalConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    project_root = _resolve_path(config_path.parent, raw.get("project_root", ".."))
    runtime_root = _resolve_path(project_root, raw.get("runtime_root", "runtime"))
    region_json_path = _resolve_path(
        project_root, raw.get("region_json_path", "naver_region_codes.json")
    )

    crawl_raw = raw.get("crawl", {})
    crawl = CrawlSettings(
        shard_total=int(crawl_raw.get("shard_total", 20)),
        shard_groups=list(crawl_raw.get("shard_groups", DEFAULT_SHARD_GROUPS)),
        max_local_parallel=int(crawl_raw.get("max_local_parallel", 2)),
        max_concurrent_pages=int(crawl_raw.get("max_concurrent_pages", 2)),
        max_api_prefetch_concurrency=int(
            crawl_raw.get("max_api_prefetch_concurrency", 4)
        ),
        retry_failed_shards=int(crawl_raw.get("retry_failed_shards", 1)),
        shard_timeout_minutes=int(crawl_raw.get("shard_timeout_minutes", 240)),
        navigation_retry_attempts=int(crawl_raw.get("navigation_retry_attempts", 3)),
        network_ready_retry_attempts=int(
            crawl_raw.get("network_ready_retry_attempts", 12)
        ),
        network_ready_retry_delay_seconds=float(
            crawl_raw.get("network_ready_retry_delay_seconds", 5)
        ),
        max_failed_region_ratio=float(crawl_raw.get("max_failed_region_ratio", 0.25)),
    )

    export_jobs = [
        ExportJob(
            name=item["name"],
            regions=list(item["regions"]),
            min_households=int(item.get("min_households", 0)),
        )
        for item in raw.get("exports", DEFAULT_EXPORTS)
    ]

    drive_raw = raw.get("google_drive", {})
    token_file_value = drive_raw.get("token_file", ".local_secrets/gdrive_token.json")
    drive = GoogleDriveSettings(
        enabled=bool(drive_raw.get("enabled", True)),
        folder_id=str(drive_raw.get("folder_id", "")).strip(),
        token_file=_resolve_path(project_root, token_file_value),
    )

    discord_raw = raw.get("discord", {})
    webhook_file_value = discord_raw.get(
        "webhook_file", ".local_secrets/discord_webhook_url.txt"
    )
    discord = DiscordSettings(
        enabled=bool(discord_raw.get("enabled", False)),
        webhook_url=str(discord_raw.get("webhook_url", "")).strip(),
        webhook_file=_resolve_path(project_root, webhook_file_value),
    )

    validator_raw = raw.get("validator", {})
    validator = ValidatorSettings(
        enabled=bool(validator_raw.get("enabled", True)),
        runtime_root=_resolve_path(
            project_root, validator_raw.get("runtime_root", "runtime/monthly_validator")
        ),
        repo_root=_resolve_path(project_root, validator_raw.get("repo_root", ".")),
        region_file=_resolve_path(
            project_root, validator_raw.get("region_file", "naver_region_codes.json")
        ),
        max_concurrent_tabs=int(validator_raw.get("max_concurrent_tabs", 5)),
        timeout_ms=int(validator_raw.get("timeout_ms", 10000)),
        expected_branch=str(validator_raw.get("expected_branch", "main")).strip(),
        remote_name=str(validator_raw.get("remote_name", "origin")).strip(),
        commit_name=str(validator_raw.get("commit_name", "Local Validator")).strip(),
        commit_email=str(
            validator_raw.get("commit_email", "local-validator@localhost")
        ).strip(),
        commit_message_template=str(
            validator_raw.get(
                "commit_message_template", "Update region validity: {date}"
            )
        ),
        push_changes=bool(validator_raw.get("push_changes", True)),
    )

    python_executable = str(raw.get("python_executable") or sys.executable)

    return LocalConfig(
        project_root=project_root,
        runtime_root=runtime_root,
        region_json_path=region_json_path,
        python_executable=python_executable,
        preflight_smoke_test=bool(raw.get("preflight_smoke_test", False)),
        retention_days=int(raw.get("retention_days", 14)),
        crawl=crawl,
        export_jobs=export_jobs,
        google_drive=drive,
        discord=discord,
        validator=validator,
    )
