from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


class LockError(RuntimeError):
    pass


@dataclass
class LockMetadata:
    pid: int
    run_id: str
    created_at: str
    command: str


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_lock(path: Path) -> LockMetadata | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return LockMetadata(**data)
    except Exception:
        return None


@contextmanager
def file_lock(path: Path, run_id: str, command: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_lock(path)
    if existing and _process_exists(existing.pid):
        raise LockError(
            f"active run exists: pid={existing.pid}, run_id={existing.run_id}, created_at={existing.created_at}"
        )
    if path.exists():
        path.unlink()

    metadata = LockMetadata(
        pid=os.getpid(),
        run_id=run_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        command=command,
    )

    with path.open("x", encoding="utf-8") as f:
        json.dump(asdict(metadata), f, ensure_ascii=False, indent=2)

    try:
        yield metadata
    finally:
        if path.exists():
            path.unlink()
