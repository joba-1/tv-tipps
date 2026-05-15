"""EPG fetch, cache, and query."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.models import Channel, EpgEvent, Receiver, Bouquet
from app.enigma.client import EnigmaClient
from app.enigma.parser import parse_epg_events
from app.timezones import utcnow
from app.logging_setup import get_logger

log = get_logger(__name__)

_STALE_SECS = 2 * 3600  # 2 hours


async def refresh_now_next(receiver: Receiver, client: EnigmaClient, db: Session) -> int:
    """Fetch epgnow + epgnext for every bouquet on this receiver."""
    bouquets = db.query(Bouquet).filter_by(receiver_id=receiver.id).all()
    if not bouquets:
        log.warning("epg.no_bouquets", receiver=receiver.name)
        return 0

    total = 0
    now = utcnow()
    seen_srefs: set[str] = set()

    for bouquet in bouquets:
        for endpoint in ("now", "next"):
            if endpoint == "now":
                raw = await client.get_epg_now(bouquet.bref)
            else:
                raw = await client.get_epg_next(bouquet.bref)

            if raw is None:
                continue

            events = parse_epg_events(raw)
            for ev in events:
                if ev.sref in seen_srefs and endpoint == "next":
                    pass  # allow next even if we saw now for same sref
                seen_srefs.add(ev.sref)

                channel = db.query(Channel).filter_by(sref=ev.sref).first()
                if channel is None:
                    continue

                stmt = (
                    sqlite_insert(EpgEvent)
                    .values(
                        channel_id=channel.id,
                        event_id=ev.event_id,
                        title=ev.title,
                        short_desc=ev.short_desc,
                        long_desc=ev.long_desc,
                        start_time=ev.start_time,
                        end_time=ev.end_time,
                        duration_sec=ev.duration_sec,
                        genre=ev.genre,
                        cached_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["channel_id", "start_time"],
                        set_={
                            "title": ev.title,
                            "short_desc": ev.short_desc,
                            "long_desc": ev.long_desc,
                            "end_time": ev.end_time,
                            "duration_sec": ev.duration_sec,
                            "event_id": ev.event_id,
                            "genre": ev.genre,
                            "cached_at": now,
                        },
                    )
                )
                db.execute(stmt)
                total += 1

    db.commit()
    log.info("epg.refreshed_now_next", receiver=receiver.name, events=total)
    return total


async def refresh_epg_service(channel: Channel, client: EnigmaClient, db: Session, hours: int = 24) -> int:
    """Fetch full EPG for one channel. Skips update if response is sparse (box just booted)."""
    raw = await client.get_epg_service(channel.sref, hours=hours)
    if raw is None:
        return 0

    now = utcnow()
    events = parse_epg_events(raw)

    # If very few events returned for a long window, box likely just booted — keep cached data
    if hours >= 12 and len(events) < 3:
        return 0

    count = 0

    for ev in events:
        stmt = (
            sqlite_insert(EpgEvent)
            .values(
                channel_id=channel.id,
                event_id=ev.event_id,
                title=ev.title,
                short_desc=ev.short_desc,
                long_desc=ev.long_desc,
                start_time=ev.start_time,
                end_time=ev.end_time,
                duration_sec=ev.duration_sec,
                cached_at=now,
            )
            .on_conflict_do_update(
                index_elements=["channel_id", "start_time"],
                set_={
                    "title": ev.title,
                    "short_desc": ev.short_desc,
                    "cached_at": now,
                },
            )
        )
        db.execute(stmt)
        count += 1

    db.commit()
    return count


def get_now_next(channel_ids: list[int], db: Session) -> list[dict]:
    """Return current + next event for each channel. channel_ids may be empty (→ all)."""
    now = utcnow()
    result = []

    channels = (
        db.query(Channel).filter(Channel.id.in_(channel_ids)).all()
        if channel_ids
        else db.query(Channel).all()
    )

    for ch in channels:
        current = (
            db.query(EpgEvent)
            .filter(
                EpgEvent.channel_id == ch.id,
                EpgEvent.start_time <= now,
                EpgEvent.end_time > now,
            )
            .order_by(EpgEvent.start_time.desc())
            .first()
        )
        nxt = (
            db.query(EpgEvent)
            .filter(
                EpgEvent.channel_id == ch.id,
                EpgEvent.start_time > now,
            )
            .order_by(EpgEvent.start_time.asc())
            .first()
        )

        stale = False
        if current and (now - current.cached_at).total_seconds() > _STALE_SECS:
            stale = True
        elif not current and not nxt:
            # No data at all
            stale = True

        result.append({
            "channel_id": ch.id,
            "channel_name": ch.name,
            "sref": ch.sref,
            "picon_path": ch.picon_path,
            "stale": stale,
            "now": _event_dict(current, now) if current else None,
            "next": _event_dict(nxt, now) if nxt else None,
        })

    return result


def get_epg_range(channel_ids: list[int], start: datetime, end: datetime, db: Session) -> list[dict]:
    """Return EPG events overlapping [start, end] for the given channels."""
    query = (
        db.query(EpgEvent, Channel)
        .join(Channel, EpgEvent.channel_id == Channel.id)
        .filter(
            EpgEvent.start_time < end,
            EpgEvent.end_time > start,
        )
        .order_by(EpgEvent.start_time.asc())
    )
    if channel_ids:
        query = query.filter(EpgEvent.channel_id.in_(channel_ids))

    now = utcnow()
    result = []
    for ev, ch in query.all():
        d = _event_dict(ev, now)
        d["channel_name"] = ch.name
        d["sref"] = ch.sref
        d["channel_id"] = ch.id
        result.append(d)
    return result


def _event_dict(ev: EpgEvent, now: datetime) -> dict:
    elapsed = max(0, (now - ev.start_time).total_seconds())
    total = ev.duration_sec or 1
    return {
        "id": ev.id,
        "event_id": ev.event_id,
        "title": ev.title,
        "short_desc": ev.short_desc,
        "long_desc": ev.long_desc,
        "start_time": ev.start_time.isoformat(),
        "end_time": ev.end_time.isoformat(),
        "duration_sec": ev.duration_sec,
        "genre": ev.genre,
        "progress_pct": round(min(100, elapsed / total * 100), 1),
    }


def cleanup_old_events(retention_days: int, db: Session) -> int:
    from sqlalchemy import text
    result = db.execute(text(
        "DELETE FROM epg_events WHERE end_time < datetime('now', :days)"
        " AND id NOT IN (SELECT epg_event_id FROM viewing_sessions WHERE epg_event_id IS NOT NULL)",
    ), {"days": f"-{retention_days} days"})
    db.commit()
    deleted = result.rowcount
    if deleted:
        log.info("epg.cleanup", deleted=deleted, retention_days=retention_days)
    return deleted
