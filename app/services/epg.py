"""EPG fetch, cache, and query."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import func, update
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.models import Channel, EpgEvent, Receiver, Bouquet, UserEventScore, ViewingSession
from app.enigma.client import EnigmaClient
from app.enigma.parser import parse_epg_events, ParsedEpgEvent, EnigmaParseError
from app.services.forensics import dump_failure
from app.services.scoring import enqueue_scoring
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


def _upsert_events(
    channel: Channel, events: list[ParsedEpgEvent], db: Session, now: datetime,
) -> tuple[int, list[int]]:
    """Upsert parsed events for one channel and detect changes to the fields
    the LLM prompt uses (genre, title, short_desc) on pre-existing rows — most
    importantly genre going from NULL to something after a richer receiver
    comes online. Known genre/desc values are never blanked out by a sparser
    fetch (coalesce). Returns (upserted_count, changed_event_ids); the caller
    stale-marks scores for the changed ids and commits."""
    if not events:
        return 0, []
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
                "genre": func.coalesce(stmt.excluded.genre, EpgEvent.genre),
                "cached_at": now,
            },
        )
        db.execute(stmt)
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

    _remove_overlapped(channel, events, db, now)
    return count, changed_event_ids


def _remove_overlapped(
    channel: Channel, events: list[ParsedEpgEvent], db: Session, now: datetime,
) -> int:
    """Unify overlapping events: delete not-yet-started rows on this channel
    that overlap a freshly fetched event but have a different start_time.
    These are leftovers of schedule shifts — the unique key is
    (channel_id, start_time), so a moved event creates a second row instead of
    updating the old one. Rows already started or referenced by a viewing
    session are left alone. Score rows cascade with the delete."""
    fetched_starts = {ev.start_time for ev in events}
    win_start = min(ev.start_time for ev in events)
    win_end = max(ev.end_time for ev in events)
    candidates = (
        db.query(EpgEvent.id, EpgEvent.start_time, EpgEvent.end_time)
        .filter(
            EpgEvent.channel_id == channel.id,
            EpgEvent.start_time > now,
            EpgEvent.start_time < win_end,
            EpgEvent.end_time > win_start,
            EpgEvent.start_time.notin_(fetched_starts),
        )
        .all()
    )
    if not candidates:
        return 0
    doomed = [
        cid for cid, c_start, c_end in candidates
        if any(c_start < ev.end_time and ev.start_time < c_end for ev in events)
    ]
    if doomed:
        referenced = {
            r[0] for r in db.query(ViewingSession.epg_event_id)
            .filter(ViewingSession.epg_event_id.in_(doomed)).all()
        }
        doomed = [i for i in doomed if i not in referenced]
    if not doomed:
        return 0
    db.query(EpgEvent).filter(EpgEvent.id.in_(doomed)).delete(synchronize_session=False)
    log.info("epg.overlap_removed", channel=channel.name, count=len(doomed))
    return len(doomed)


def _enqueue_future_events(channel_id: int, start_times: list[datetime],
                           now: datetime, db: Session) -> None:
    """Resolve (channel, start_time) pairs back to ids of still-future events
    and queue them for background scoring. The scorer skips rows that are
    already fresh, so re-queueing merely-updated events is harmless."""
    if not start_times:
        return
    ids = [
        row[0] for row in db.query(EpgEvent.id).filter(
            EpgEvent.channel_id == channel_id,
            EpgEvent.start_time.in_(start_times),
            EpgEvent.end_time > now,
        ).all()
    ]
    if ids:
        enqueue_scoring(ids)


async def refresh_now_next(receiver: Receiver, client: EnigmaClient, db: Session) -> int:
    """Fetch epgnow + epgnext for every bouquet on this receiver."""
    bouquets = db.query(Bouquet).filter_by(receiver_id=receiver.id).all()
    if not bouquets:
        log.warning("epg.no_bouquets", receiver=receiver.name)
        return 0

    now = utcnow()
    # Collect per channel, deduped by start_time — the same event shows up via
    # multiple bouquets and via both endpoints at slot boundaries.
    pending: dict[int, dict[datetime, ParsedEpgEvent]] = {}
    channels: dict[int, Channel] = {}
    by_sref: dict[str, Channel | None] = {}

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
                if ev.sref not in by_sref:
                    by_sref[ev.sref] = db.query(Channel).filter_by(sref=ev.sref).first()
                channel = by_sref[ev.sref]
                if channel is None:
                    continue
                channels[channel.id] = channel
                pending.setdefault(channel.id, {})[ev.start_time] = ev

    total = 0
    changed_event_ids: list[int] = []
    for ch_id, evmap in pending.items():
        count, changed = _upsert_events(channels[ch_id], list(evmap.values()), db, now)
        total += count
        changed_event_ids.extend(changed)

    if changed_event_ids:
        n = _stale_scores_for_events(db, changed_event_ids)
        log.info("epg.stale_marked_on_change",
                 receiver=receiver.name, events=len(changed_event_ids), rows=n)

    db.commit()

    for ch_id, evmap in pending.items():
        _enqueue_future_events(ch_id, list(evmap.keys()), now, db)

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

    count, changed_event_ids = _upsert_events(channel, events, db, now)

    if changed_event_ids:
        n = _stale_scores_for_events(db, changed_event_ids)
        log.info("epg.stale_marked_on_change",
                 channel=channel.name, events=len(changed_event_ids), rows=n)

    db.commit()

    _enqueue_future_events(channel.id, [ev.start_time for ev in events], now, db)
    return count


def transponder_key(sref: str) -> tuple[str, str, str] | None:
    """(TSID, ONID, namespace) from a service reference — enigma2's identity for
    the transponder a channel sits on. Fields are
    type:flags:serviceType:SID:TSID:ONID:namespace:…"""
    parts = sref.split(":")
    if len(parts) < 7:
        return None
    return parts[4].upper(), parts[5].upper(), parts[6].upper()


def is_dvb_service(sref: str) -> bool:
    """Is this a service the tuner can actually tune to?

    Bouquets also carry pseudo-services that no zap will ever produce EPG for —
    an AV input appears as `8192:0:1:0:0:0:0:0:0:0::PS3`, streams use type 4097.
    A real DVB reference has type 1 and a non-zero transponder triple. Zapping
    the others just burns a dwell per night on a PlayStation input."""
    parts = sref.split(":")
    if len(parts) < 7 or parts[0] != "1":
        return False
    return transponder_key(sref) != ("0", "0", "0")


def transponder_groups(channels: list[Channel]) -> list[list[Channel]]:
    """Channels grouped by the transponder they sit on, each group ordered and
    the groups themselves ordered by first channel name — deterministic, so
    consecutive nights take the same route and the logs stay comparable.
    Non-tunable entries are dropped; a channel that merely has no EPG yet is
    kept, since that can change."""
    by_tp: dict[tuple[str, str, str], list[Channel]] = {}
    for ch in sorted(channels, key=lambda c: (c.name or "", c.sref)):
        if not is_dvb_service(ch.sref):
            continue
        key = transponder_key(ch.sref)
        if key:
            by_tp.setdefault(key, []).append(ch)
    return list(by_tp.values())


def transponder_tour(channels: list[Channel]) -> list[Channel]:
    """One channel per distinct transponder — the representative we zap to."""
    return [group[0] for group in transponder_groups(channels)]


async def _count_cached_events(client: EnigmaClient, group: list[Channel]) -> int:
    """How many EPG events the box currently holds for this transponder's
    channels. Read with the same call the sweep uses, so the number we watch
    saturate is the number the sweep will actually harvest."""
    total = 0
    for ch in group:
        raw = await client.get_epg_service(ch.sref, hours=24)
        if not raw:
            continue
        try:
            total += len(parse_epg_events(raw))
        except EnigmaParseError:
            # A malformed payload must not read as growth.
            continue
    return total


async def _dwell_until_saturated(
    client: EnigmaClient, group: list[Channel], *,
    min_sec: float, max_sec: float, flat_sec: float, sample_sec: float,
) -> dict:
    """Sit on the current transponder until its EPG stops growing.

    EIT arrives on a carousel, so the event count climbs and then goes flat. A
    single flat sample is not saturation — sections arrive in bursts and a lull
    mid-cycle looks identical to "done" — so we require `flat_sec` without a new
    event before moving on. `min_sec` guards against declaring victory before the
    first sections land; `max_sec` caps a transponder that never settles.
    """
    import asyncio
    import math

    def _samples(seconds: float) -> int:
        """How many samples span `seconds`. The loop counts samples rather than
        seconds so "flat for flat_sec" always means at least one observed flat
        sample, however the bounds are configured."""
        if sample_sec <= 0:
            return 1 if seconds > 0 else 0
        return max(0, math.ceil(seconds / sample_sec))

    flat_needed = max(1, _samples(flat_sec))
    min_samples = _samples(min_sec)
    max_samples = max(1, _samples(max_sec))

    loop = asyncio.get_running_loop()
    start = loop.time()
    best = -1
    last_growth = 0.0
    taken = 0
    flat = 0
    curve: list[tuple[int, int]] = []
    reason = "ceiling"
    while True:
        await asyncio.sleep(sample_sec)
        taken += 1
        elapsed = loop.time() - start
        count = await _count_cached_events(client, group)
        curve.append((round(elapsed), count))
        if count > best:
            best = count
            last_growth = elapsed
            flat = 0
        else:
            flat += 1
        if taken >= min_samples and flat >= flat_needed:
            reason = "saturated"
            break
        if taken >= max_samples:
            break
    return {
        "events": max(best, 0),
        "dwell_sec": round(curve[-1][0]) if curve else 0,
        "saturated_after_sec": round(last_growth) if best > 0 else None,
        "reason": reason,
        # Kept small enough to log: it is the evidence for tuning the bounds.
        "curve": curve[:40],
    }


async def prime_epg_cache(
    receiver: Receiver, client: EnigmaClient, channels: list[Channel], *,
    min_sec: float, max_sec: float, flat_sec: float, sample_sec: float,
    tour_max_sec: float = 1800,
) -> dict:
    """Zap through one channel per transponder so enigma2 reads each one's EPG
    into its cache before we sweep it, dwelling on each until its EPG stops
    growing.

    A box that was mains-cut boots with an empty EPG cache — enigma2 only gets a
    chance to write /etc/enigma2/epg.dat on a clean shutdown — and it can only
    see EPG for the transponder its tuner currently sits on. Without this tour a
    freshly woken box yields exactly one transponder's worth of data (measured:
    13 of 2830 channels, all RTL-group).

    The dwell is adaptive because a fixed one is wrong in both directions: a
    single-channel transponder saturates in seconds while the 8-channel one needs
    far longer, and broadcasters change how much schedule they transmit. Each
    transponder's saturation time is logged, so the bounds can be re-tuned from
    real numbers rather than guesswork.

    Zapping works in light standby and does not bring the box out of it, so the
    HDMI output stays dark for the whole tour. Only ever call this for a box we
    powered up ourselves — never zap a receiver someone is watching.
    """
    import asyncio

    groups = transponder_groups(channels)
    log.info("epg.prime_start", receiver=receiver.name, transponders=len(groups),
             channels=len(channels), min_sec=min_sec, max_sec=max_sec,
             flat_sec=flat_sec)
    loop = asyncio.get_running_loop()
    tour_start = loop.time()
    visited = 0
    saturated_times: list[int] = []
    hit_ceiling = 0
    unvisited = 0
    for group in groups:
        rep = group[0]
        key = transponder_key(rep.sref)
        if loop.time() - tour_start >= tour_max_sec:
            # Out of budget: the rest keeps its stale data for one more night,
            # which beats keeping the box powered indefinitely.
            unvisited = len(groups) - visited
            log.warning("epg.prime_budget_exhausted", receiver=receiver.name,
                        visited=visited, unvisited=unvisited,
                        tour_max_sec=tour_max_sec)
            break
        if not await client.zap(rep.sref):
            log.info("epg.prime_zap_failed", receiver=receiver.name, channel=rep.name)
            continue
        visited += 1
        stats = await _dwell_until_saturated(
            client, group, min_sec=min_sec, max_sec=max_sec,
            flat_sec=flat_sec, sample_sec=sample_sec)
        if stats["reason"] == "ceiling":
            hit_ceiling += 1
        if stats["saturated_after_sec"] is not None:
            saturated_times.append(stats["saturated_after_sec"])
        log.info("epg.prime_transponder", receiver=receiver.name,
                 transponder=":".join(key) if key else None,
                 channel=rep.name, channels=len(group), **stats)
    total_sec = round(loop.time() - tour_start)
    log.info("epg.prime_done", receiver=receiver.name,
             visited=visited, transponders=len(groups), total_sec=total_sec,
             hit_ceiling=hit_ceiling, unvisited=unvisited,
             slowest_saturation_sec=max(saturated_times, default=None))
    return {"transponders": len(groups), "visited": visited,
            "total_sec": total_sec, "hit_ceiling": hit_ceiling,
            "unvisited": unvisited}


def get_now_next(channel_ids: list[int], db: Session) -> list[dict]:
    """Return current + next event for each channel. channel_ids may be empty (→ all).
    Three set-based queries instead of two per channel."""
    from sqlalchemy import tuple_
    now = utcnow()

    channels = (
        db.query(Channel).filter(Channel.id.in_(channel_ids)).all()
        if channel_ids
        else db.query(Channel).all()
    )
    ids = [ch.id for ch in channels]

    # Currently airing: ascending start order, so the last write per channel is
    # the most recent start — matches the old per-channel DESC-first pick.
    current_by_ch: dict[int, EpgEvent] = {}
    for ev in (
        db.query(EpgEvent)
        .filter(
            EpgEvent.channel_id.in_(ids),
            EpgEvent.start_time <= now,
            EpgEvent.end_time > now,
        )
        .order_by(EpgEvent.start_time.asc())
        .all()
    ):
        current_by_ch[ev.channel_id] = ev

    # Upcoming: earliest future start per channel, resolved in one tuple-IN fetch.
    next_starts = (
        db.query(EpgEvent.channel_id, func.min(EpgEvent.start_time))
        .filter(EpgEvent.channel_id.in_(ids), EpgEvent.start_time > now)
        .group_by(EpgEvent.channel_id)
        .all()
    )
    next_by_ch: dict[int, EpgEvent] = {}
    if next_starts:
        for ev in (
            db.query(EpgEvent)
            .filter(tuple_(EpgEvent.channel_id, EpgEvent.start_time).in_(list(next_starts)))
            .all()
        ):
            next_by_ch.setdefault(ev.channel_id, ev)

    result = []
    for ch in channels:
        current = current_by_ch.get(ch.id)
        nxt = next_by_ch.get(ch.id)
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
    cleanup_overlapping_events(db)
    return deleted


def cleanup_overlapping_events(db: Session) -> int:
    """Catch-all for overlap unification: among future events that overlap on
    the same channel, keep the most recently cached row (id as tie-break) and
    delete the rest. The ingest path already removes most overlaps; this
    sweeps up whatever slipped through (e.g. shifts on channels a sweep never
    re-fetched). Session-referenced rows are exempt."""
    from sqlalchemy import text
    now_str = utcnow().isoformat(sep=" ")
    result = db.execute(text(
        "DELETE FROM epg_events WHERE id IN ("
        " SELECT a.id FROM epg_events a JOIN epg_events b"
        "   ON b.channel_id = a.channel_id AND b.id != a.id"
        "   AND b.start_time < a.end_time AND a.start_time < b.end_time"
        " WHERE a.start_time > :now"
        "   AND (a.cached_at < b.cached_at"
        "        OR (a.cached_at = b.cached_at AND a.id < b.id))"
        "   AND a.id NOT IN (SELECT epg_event_id FROM viewing_sessions"
        "                    WHERE epg_event_id IS NOT NULL))",
    ), {"now": now_str})
    db.commit()
    deleted = result.rowcount or 0
    if deleted:
        log.info("epg.overlap_cleanup", deleted=deleted)
    return deleted
