"""EPG fetch, cache, and query."""
from __future__ import annotations
import asyncio
from datetime import datetime
from sqlalchemy import func, update
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.models import Channel, EpgEvent, Receiver, Bouquet, UserEventScore
from app.enigma.client import EnigmaClient
from app.enigma.parser import parse_epg_events, EnigmaParseError
from app.services.forensics import dump_failure
from app.timezones import utcnow
from app.logging_setup import get_logger

log = get_logger(__name__)

_STALE_SECS = 3 * 3600  # 3 hours


def _stale_scores_for_events(db: Session, event_ids: list[int]) -> int:
    """Mark all user_event_scores rows for these events as stale so the next
    scoring pass re-rates them. Returns number of rows touched."""
    if not event_ids:
        return 0
    result = db.execute(
        update(UserEventScore)
        .where(UserEventScore.epg_event_id.in_(event_ids))
        .values(stale=True)
    )
    return result.rowcount or 0


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

            try:
                events = parse_epg_events(raw)
            except EnigmaParseError as e:
                dump_failure("enigma", tag=f"{receiver.name}_epg{endpoint}",
                             receiver=receiver.name, endpoint=f"epg{endpoint}",
                             bref=bouquet.bref, error=str(e), raw=raw)
                log.warning("epg.parse_failed", receiver=receiver.name,
                            endpoint=endpoint, error=str(e))
                continue
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
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["channel_id", "start_time"],
                    set_={
                        "title": ev.title,
                        "short_desc": ev.short_desc,
                        "long_desc": ev.long_desc,
                        "end_time": ev.end_time,
                        "duration_sec": ev.duration_sec,
                        "event_id": ev.event_id,
                        # Don't blank out a known genre with NULL — receivers
                        # without has_genre return None even for events another
                        # receiver already enriched.
                        "genre": func.coalesce(stmt.excluded.genre, EpgEvent.genre),
                        "cached_at": now,
                    },
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
    try:
        events = parse_epg_events(raw)
    except EnigmaParseError as e:
        dump_failure("enigma", tag=f"epgservice_{channel.sref[:24]}",
                     endpoint="epgservice", sref=channel.sref,
                     error=str(e), raw=raw)
        log.warning("epg.parse_failed_service", sref=channel.sref, error=str(e))
        return 0

    # If very few events returned for a long window, box likely just booted — keep cached data
    if hours >= 12 and len(events) < 3:
        return 0

    count = 0
    touched_keys: list[tuple[int, datetime]] = []

    # Pre-fetch existing rows so we can detect changes to fields that the LLM
    # prompt uses (genre, title, short_desc). When any of those changes — most
    # importantly genre going from NULL to something after a richer receiver
    # comes online — we mark the matching user_event_scores stale so they get
    # re-rated against the better data.
    start_times = [ev.start_time for ev in events]
    existing = {
        (row[0], row[1]): (row[2], row[3], row[4], row[5])
        for row in db.query(
            EpgEvent.channel_id, EpgEvent.start_time,
            EpgEvent.id, EpgEvent.genre, EpgEvent.title, EpgEvent.short_desc,
        ).filter(
            EpgEvent.channel_id == channel.id,
            EpgEvent.start_time.in_(start_times),
        ).all()
    }
    changed_event_ids: list[int] = []

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
                genre=ev.genre,
                cached_at=now,
            )
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["channel_id", "start_time"],
            set_={
                "title": ev.title,
                "short_desc": func.coalesce(stmt.excluded.short_desc, EpgEvent.short_desc),
                "long_desc": func.coalesce(stmt.excluded.long_desc, EpgEvent.long_desc),
                "end_time": ev.end_time,
                "duration_sec": ev.duration_sec,
                "event_id": ev.event_id,
                # Preserve a previously-known genre when the new fetch lacks one.
                "genre": func.coalesce(stmt.excluded.genre, EpgEvent.genre),
                "cached_at": now,
            },
        )
        db.execute(stmt)
        touched_keys.append((channel.id, ev.start_time))
        count += 1

        prev = existing.get((channel.id, ev.start_time))
        if prev:
            prev_id, prev_genre, prev_title, prev_short = prev
            # Coalesce mirrors what the upsert actually stored.
            post_genre = ev.genre if ev.genre is not None else prev_genre
            post_short = ev.short_desc if ev.short_desc is not None else prev_short
            if (post_genre != prev_genre
                or ev.title != prev_title
                or (post_short or "") != (prev_short or "")):
                changed_event_ids.append(prev_id)

    if changed_event_ids:
        n = _stale_scores_for_events(db, changed_event_ids)
        log.info("epg.stale_marked_on_change",
                 channel=channel.name, events=len(changed_event_ids), rows=n)

    db.commit()

    # Resolve the (channel, start_time) pairs back to EpgEvent ids and fire a
    # background scoring task. The scorer scores stale rows and rows with no
    # entry yet, so retracing updated events here is harmless.
    if touched_keys:
        ids = [
            row[0] for row in db.query(EpgEvent.id).filter(
                EpgEvent.channel_id == channel.id,
                EpgEvent.start_time.in_([k[1] for k in touched_keys]),
                EpgEvent.end_time > now,
            ).all()
        ]
        if ids:
            from app.services.scoring import score_events_for_all_users
            asyncio.create_task(score_events_for_all_users(ids))
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

        stale = bool(current and (now - current.cached_at).total_seconds() > _STALE_SECS)

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


def get_epg_range(
    channel_ids: list[int], start: datetime, end: datetime, db: Session,
    future_only: bool = False,
) -> list[dict]:
    """Return EPG events for the given channels in [start, end].
    future_only=True: only shows that haven't started yet (start_time >= start).
    future_only=False (default): overlap semantics — includes currently-airing shows."""
    start_filter = (EpgEvent.start_time >= start) if future_only else (EpgEvent.end_time > start)
    query = (
        db.query(EpgEvent, Channel)
        .join(Channel, EpgEvent.channel_id == Channel.id)
        .filter(
            EpgEvent.start_time < end,
            start_filter,
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
