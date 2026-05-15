"""User viewing profile: compute from confirmed session history, cache 24h in DB."""
from __future__ import annotations
import json
from collections import Counter
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session
from app.models import ViewingSession, EpgEvent, Channel, UserProfile
from app.timezones import utcnow
from app.logging_setup import get_logger
from config import settings

log = get_logger(__name__)

PROFILE_WINDOW_DAYS = 30


def compute_profile(user_id: int, db: Session) -> dict:
    """Query confirmed sessions and aggregate into a profile dict, then persist."""
    cutoff = utcnow() - timedelta(days=PROFILE_WINDOW_DAYS)
    sessions = (
        db.query(ViewingSession)
        .filter(
            ViewingSession.user_id == user_id,
            ViewingSession.confirmed == True,  # noqa: E712
            ViewingSession.started_at >= cutoff,
        )
        .all()
    )

    genre_counts: Counter = Counter()
    channel_counts: Counter = Counter()
    channel_srefs: dict[str, str] = {}
    durations: list[int] = []
    time_buckets = {"morning": 0, "afternoon": 0, "evening": 0, "late": 0}
    tz = ZoneInfo(settings.timezone)

    for s in sessions:
        if s.epg_event_id:
            ev = db.get(EpgEvent, s.epg_event_id)
            genre_counts[ev.genre if ev and ev.genre else "unknown"] += 1
        else:
            genre_counts["unknown"] += 1

        ch = db.get(Channel, s.channel_id)
        if ch:
            channel_counts[ch.name] += 1
            channel_srefs[ch.name] = ch.sref

        if s.duration_sec and s.duration_sec > 0:
            durations.append(s.duration_sec)

        local_dt = s.started_at.replace(tzinfo=timezone.utc).astimezone(tz)
        h = local_dt.hour
        if 6 <= h < 12:
            time_buckets["morning"] += 1
        elif 12 <= h < 18:
            time_buckets["afternoon"] += 1
        elif 18 <= h < 22:
            time_buckets["evening"] += 1
        else:
            time_buckets["late"] += 1

    total = len(sessions)
    top_genres = [
        {"genre": g, "count": c, "share": round(c / max(total, 1), 2)}
        for g, c in genre_counts.most_common(8)
    ]
    top_channels = [
        {"name": n, "sref": channel_srefs.get(n, ""), "count": c, "share": round(c / max(total, 1), 2)}
        for n, c in channel_counts.most_common(10)
    ]
    avg_duration_min = round(sum(durations) / max(len(durations), 1) / 60, 1)

    profile = {
        "session_count": total,
        "top_genres": top_genres,
        "top_channels": top_channels,
        "avg_duration_min": avg_duration_min,
        "time_buckets": time_buckets,
    }

    now = utcnow()
    existing = db.get(UserProfile, user_id)
    if existing:
        existing.computed_at = now
        existing.summary_json = json.dumps(profile)
    else:
        db.add(UserProfile(user_id=user_id, computed_at=now, summary_json=json.dumps(profile)))
    db.commit()
    log.info("profile.computed", user_id=user_id, sessions=total)
    return profile


def get_profile(user_id: int, db: Session) -> dict:
    """Return cached profile (< 24h) or recompute."""
    cached = db.get(UserProfile, user_id)
    if cached and (utcnow() - cached.computed_at).total_seconds() < 86400:
        return json.loads(cached.summary_json)
    return compute_profile(user_id, db)
