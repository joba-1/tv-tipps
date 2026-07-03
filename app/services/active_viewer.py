"""In-memory registry mapping receiver → (user_slug, last_action_at).

Populated by user-initiated remote actions (zap, key, timer, screenshot)
that carry both a user cookie and a receiver. Read by the poller's
session attribution to prefer the browser user over the receiver's
default_user. Lost on restart by design — next browser command rearms it.
"""
from __future__ import annotations
from datetime import datetime, timedelta
import threading
from app.timezones import utcnow

_TTL = timedelta(hours=4)
_lock = threading.Lock()
_active: dict[str, tuple[str, datetime]] = {}


def record_active(receiver_name: str | None, user_slug: str | None) -> None:
    if not receiver_name or not user_slug:
        return
    with _lock:
        _active[receiver_name] = (user_slug, utcnow())


def get_active(receiver_name: str) -> str | None:
    with _lock:
        entry = _active.get(receiver_name)
        if not entry:
            return None
        slug, ts = entry
        if utcnow() - ts > _TTL:
            _active.pop(receiver_name, None)
            return None
        return slug


def snapshot() -> dict[str, dict]:
    """Admin-status view: current registry with ages in seconds."""
    now = utcnow()
    out: dict[str, dict] = {}
    with _lock:
        for name, (slug, ts) in list(_active.items()):
            age = (now - ts).total_seconds()
            if age > _TTL.total_seconds():
                _active.pop(name, None)
                continue
            out[name] = {"user": slug, "age_sec": int(age)}
    return out
