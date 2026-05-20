"""On-disk forensics for parse/handler failures at trust boundaries.

Each kind (ollama, enigma, request, ...) writes to
``<db_dir>/<kind>_failures/<ts>_<pid>_<tag>.json`` with a self-rotating
window of the newest DUMP_KEEP files. Disk errors are silent — forensics
never breaks the hot path."""
from __future__ import annotations
import json
import os
import re
import time
from pathlib import Path
from app.logging_setup import get_logger
from config import settings

log = get_logger(__name__)

DUMP_KEEP = 30
_TAG_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def _dump_dir(kind: str) -> Path | None:
    db = Path(settings.db_path)
    base = db.parent if db.parent.as_posix() not in ("", ".") else Path.cwd()
    d = base / f"{kind}_failures"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None


def dump_failure(kind: str, *, tag: str = "", **payload) -> str | None:
    """Drop a `{ts, **payload}` JSON file under `<db_dir>/<kind>_failures/`.

    `tag` (short alnum identifier) is appended to the filename for grep-ability.
    Returns the written path on success, None on disk error. Self-rotates."""
    d = _dump_dir(kind)
    if d is None:
        return None
    try:
        ts = time.strftime("%Y%m%dT%H%M%S")
        safe_tag = _TAG_SAFE.sub("_", tag)[:32]
        path = d / f"{ts}_{os.getpid()}_{safe_tag or 'x'}.json"
        path.write_text(json.dumps({"ts": ts, **payload}, ensure_ascii=False,
                                   indent=2, default=str))
        # Rotate: keep newest DUMP_KEEP
        files = sorted(d.glob("*.json"))
        for old in files[:-DUMP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
        log.info(f"forensics.{kind}_dumped", path=str(path), tag=tag or None)
        return str(path)
    except (OSError, TypeError, ValueError) as e:
        log.warning(f"forensics.{kind}_dump_failed", error=str(e), tag=tag or None)
        return None
