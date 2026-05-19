"""APScheduler background jobs and per-receiver polling state machine."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import Receiver
from app.enigma.client import EnigmaClient
from app.services.channels import refresh_channels
from app.services.epg import refresh_now_next, cleanup_old_events
from app.logging_setup import get_logger
from app.timezones import utcnow
from config import settings

log = get_logger(__name__)

# In-memory state per receiver (keyed by receiver.name)
_state: dict[str, dict[str, Any]] = {}

from zoneinfo import ZoneInfo as _ZoneInfo
scheduler = AsyncIOScheduler(timezone=_ZoneInfo(settings.timezone))


def _get_state(name: str) -> dict[str, Any]:
    if name not in _state:
        _state[name] = {
            "last_sref": None,
            "session_id": None,
            "session_start": None,
            "confirmed": False,
            "last_successful_poll": None,
            "power_state": "unknown",
        }
    return _state[name]


async def _poll_receiver(receiver_name: str) -> None:
    db = SessionLocal()
    try:
        receiver = db.query(Receiver).filter_by(name=receiver_name).first()
        if not receiver:
            return

        from app.services.receivers import _to_rcfg
        rcfg = _to_rcfg(receiver)
        client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
        state = _get_state(receiver_name)
        now = utcnow()

        # Check power state
        power = await client.get_power_state()
        state["power_state"] = power
        receiver.power_state = power

        if power != "on":
            if state["session_id"]:
                if state["confirmed"]:
                    _close_session(state, now, db)
                else:
                    # Discard unconfirmed session: clear state so a later wake doesn't
                    # confirm it based on a stale started_at.
                    state["session_id"] = None
                    state["session_start"] = None
                    state["confirmed"] = False
                    state["last_sref"] = None
            db.commit()
            log.debug("poller.standby", receiver=receiver_name, power=power)
            return

        # Check current service
        raw_current = await client.get_current()
        if raw_current is None:
            # Offline
            last = state["last_successful_poll"]
            if last and (now - last).total_seconds() > 600 and state["session_id"]:
                _close_session(state, last + timedelta(seconds=settings.poll_interval_sec), db)
                db.commit()
            log.debug("poller.offline", receiver=receiver_name)
            return

        state["last_successful_poll"] = now
        receiver.last_seen = now

        from app.enigma.parser import parse_current
        current = parse_current(raw_current)
        sref = current.sref if current else None

        if sref != state["last_sref"]:
            # Channel changed
            if state["session_id"] and state["confirmed"]:
                _close_session(state, now, db)

            if sref:
                session_id = _open_session(receiver, sref, "ir_remote", now, db)
                state["session_id"] = session_id
                state["session_start"] = now
                state["confirmed"] = False
                log.info("poller.channel_change", receiver=receiver_name, sref=sref)
            else:
                state["session_id"] = None
                state["session_start"] = None
                state["confirmed"] = False

            state["last_sref"] = sref

        else:
            # Same channel
            if state["session_id"] and not state["confirmed"] and state["session_start"]:
                elapsed = (now - state["session_start"]).total_seconds()
                if elapsed >= settings.min_watch_sec:
                    _confirm_session(state, receiver, db)

        db.commit()
    except Exception as e:
        log.error("poller.error", receiver=receiver_name, error=str(e))
        db.rollback()
    finally:
        db.close()


def _open_session(receiver: Receiver, sref: str, source: str, now: datetime, db: Session) -> int | None:
    from app.models import Channel, ViewingSession
    channel = db.query(Channel).filter_by(sref=sref).first()
    if not channel:
        return None
    session = ViewingSession(
        channel_id=channel.id,
        receiver_id=receiver.id,
        started_at=now,
        source=source,
        confirmed=False,
    )
    db.add(session)
    db.flush()
    return session.id


def _confirm_session(state: dict, receiver: Receiver, db: Session) -> None:
    from app.models import ViewingSession, EpgEvent, Channel
    session = db.get(ViewingSession, state["session_id"])
    if not session:
        return
    session.confirmed = True

    # Attribution priority:
    #   1. active_viewer registry — a user-driven remote action (zap/key/timer)
    #      on this receiver in the last 4h: high-confidence browser attribution.
    #   2. receiver.default_user — IR remote fallback: who usually sits there.
    from app.models import User
    from app.services.active_viewer import get_active
    attributed = False
    active_slug = get_active(receiver.name)
    if active_slug:
        user = db.query(User).filter_by(slug=active_slug).first()
        if user:
            session.user_id = user.id
            session.confidence = 1.0
            session.attribution_method = "browser_active"
            attributed = True
    if not attributed and receiver.default_user:
        user = db.query(User).filter_by(slug=receiver.default_user).first()
        if user:
            session.user_id = user.id
            session.confidence = 0.4   # IR remote: primary user but not exclusive
            session.attribution_method = "location_heuristic"

    # Link EPG event — at slot boundaries multiple events may overlap; pick the most recent start.
    channel = db.get(Channel, session.channel_id)
    if channel:
        epg_event = (
            db.query(EpgEvent)
            .filter(
                EpgEvent.channel_id == channel.id,
                EpgEvent.start_time <= session.started_at,
                EpgEvent.end_time > session.started_at,
            )
            .order_by(EpgEvent.start_time.desc())
            .first()
        )
        if epg_event:
            session.epg_event_id = epg_event.id

    state["confirmed"] = True
    log.info("poller.session_confirmed", session_id=state["session_id"],
             receiver=receiver.name, user_id=session.user_id,
             attribution=session.attribution_method)


def _close_session(state: dict, ended_at: datetime, db: Session) -> None:
    from app.models import ViewingSession
    session = db.get(ViewingSession, state["session_id"])
    if session:
        session.ended_at = ended_at
        if session.started_at:
            session.duration_sec = int((ended_at - session.started_at).total_seconds())
    state["session_id"] = None
    state["session_start"] = None
    state["confirmed"] = False


_last_full_sweep_at: dict[str, datetime] = {}  # receiver_name → last full sweep
_skipped_channels: dict[str, set[int]] = {}    # receiver_name → channel_ids skipped last sweep
_consecutive_empty: dict[int, int] = {}        # channel_id → consecutive full sweeps with no future EPG
# Threshold for the "overdue" promotion (<24h so a missed 03:30 cron is picked
# up by the next hourly refresh that finds a box online).
_FULL_SWEEP_OVERDUE_HOURS = 23
# Log a channel as "out of EPG" only after this many consecutive sweeps with
# no future events — filters single-event misses and the post-restart cold start.
_EMPTY_SWEEP_WARN_THRESHOLD = 2


async def _refresh_all_epg(full: bool = False) -> None:
    """Refresh EPG from every online receiver.
    Now/next refresh always runs on online boxes. Full sweep runs per-receiver:
    daily 03:30 + opportunistic catch-up if that receiver's own last full sweep
    is overdue. Channels skipped because the box returned sparse data (cold OWIF
    cache) are retried first on the next sweep."""
    db = SessionLocal()
    try:
        from app.services.receivers import get_receiver_configs
        from app.models import Channel, BouquetChannel, Bouquet
        from app.services.epg import refresh_epg_service
        receivers_cfg = get_receiver_configs(db)
        online_receivers: list[tuple[Receiver, EnigmaClient, object]] = []
        for rcfg in receivers_cfg:
            receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
            if not receiver:
                continue
            client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
            if await client.is_online():
                await refresh_now_next(receiver, client, db)
                online_receivers.append((receiver, client, rcfg))
            else:
                log.info("epg.receiver_offline_skip", receiver=rcfg.name)

        for receiver, client, rcfg in online_receivers:
            last = _last_full_sweep_at.get(receiver.name)
            overdue = (
                last is None
                or (utcnow() - last).total_seconds() > _FULL_SWEEP_OVERDUE_HOURS * 3600
            )
            should_sweep = full or overdue
            if not should_sweep:
                continue
            if not full:
                log.info("epg.full_sweep_catchup", receiver=receiver.name,
                         last_full=str(last))

            bouquet_ids = [b.id for b in db.query(Bouquet).filter_by(receiver_id=receiver.id).all()]
            channel_ids = {
                bc.channel_id for bc in
                db.query(BouquetChannel).filter(BouquetChannel.bouquet_id.in_(bouquet_ids)).all()
            }
            channels = db.query(Channel).filter(Channel.id.in_(channel_ids)).all()
            # Retry previously-skipped channels first — most likely to flip to
            # populated once OWIF has settled or the user has zapped through.
            prev_skipped = _skipped_channels.get(receiver.name, set())
            channels.sort(key=lambda c: 0 if c.id in prev_skipped else 1)

            log.info("epg.full_refresh_start", receiver=receiver.name,
                     channels=len(channels), retry_priority=len(prev_skipped))
            skipped: set[int] = set()
            for ch in channels:
                count = await refresh_epg_service(ch, client, db, hours=24)
                if count == 0:
                    skipped.add(ch.id)
            _skipped_channels[receiver.name] = skipped
            _last_full_sweep_at[receiver.name] = utcnow()
            log.info("epg.full_refresh_done", receiver=receiver.name,
                     skipped=len(skipped))
            _check_empty_epg(receiver, channel_ids, db)
    except Exception as e:
        log.error("epg.refresh_error", error=str(e))
    finally:
        db.close()


def _check_empty_epg(receiver: Receiver, channel_ids: set[int], db: Session) -> None:
    """After a full sweep: flag channels whose DB has no future events.
    A channel is only logged once it has been empty for several sweeps in a row,
    so single misses and post-restart cold starts don't spam."""
    from app.models import Channel, EpgEvent
    from sqlalchemy import func as _func
    now = utcnow()
    rows = (
        db.query(Channel.id, Channel.name,
                 _func.max(EpgEvent.end_time).label("max_end"))
        .outerjoin(EpgEvent, EpgEvent.channel_id == Channel.id)
        .filter(Channel.id.in_(channel_ids))
        .group_by(Channel.id)
        .all()
    )
    flagged: list[dict] = []
    for ch_id, name, max_end in rows:
        empty = (max_end is None) or (max_end <= now)
        if empty:
            n = _consecutive_empty.get(ch_id, 0) + 1
            _consecutive_empty[ch_id] = n
            if n >= _EMPTY_SWEEP_WARN_THRESHOLD:
                flagged.append({"channel_id": ch_id, "name": name,
                                "sweeps_empty": n,
                                "last_event_end": str(max_end) if max_end else None})
        else:
            _consecutive_empty.pop(ch_id, None)
    if flagged:
        log.warning("epg.channels_out_of_data",
                    receiver=receiver.name, count=len(flagged),
                    channels=flagged[:40])  # cap log payload
    # Warm recommendation cache in background — must NOT block EPG refresh or
    # startup; each LLM call takes seconds and we warm all contexts × users.
    asyncio.create_task(_warm_recommendation_cache())


_warming = False
_WARM_CONTEXTS = ("now", "next", "prime", "today")


async def _warm_recommendation_cache() -> None:
    """Pre-generate recommendations for every user × every context. LLM cost
    happens on this background path so user requests hit a warm cache. Calls
    are serial (Ollama is single-threaded); overlapping invocations are skipped."""
    global _warming
    if _warming:
        log.info("recs.warm_skip_already_running")
        return
    _warming = True
    try:
        from app.models import User
        from app.services.recommendations import get_recommendations
        db = SessionLocal()
        try:
            users = db.query(User).all()
            for u in users:
                for ctx in _WARM_CONTEXTS:
                    try:
                        await get_recommendations(u.id, u.name, ctx, db, force_refresh=True)
                        log.info("recs.warmed", user=u.slug, context=ctx)
                    except Exception as e:
                        log.warning("recs.warm_failed", user=u.slug, context=ctx, error=str(e))
        finally:
            db.close()
    finally:
        _warming = False


async def _refresh_all_channels() -> None:
    db = SessionLocal()
    try:
        from app.services.receivers import get_receiver_configs
        for rcfg in get_receiver_configs(db):
            receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
            if not receiver:
                continue
            client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
            if not await client.is_online():
                continue
            await refresh_channels(receiver, client, db)
    except Exception as e:
        log.error("channels.refresh_error", error=str(e))
    finally:
        db.close()


async def _cleanup_epg() -> None:
    db = SessionLocal()
    try:
        cleanup_old_events(settings.epg_retention_days, db)
    finally:
        db.close()


async def run_refresh(target: str) -> None:
    """Called from admin endpoint for on-demand refresh."""
    if target in ("channels", "all"):
        await _refresh_all_channels()
    if target in ("epg", "epg_full", "all"):
        full = target == "epg_full"
        await _refresh_all_epg(full=full)


def _add_poll_job(name: str, delay_sec: int = 0) -> None:
    job_id = f"poll_{name}"
    if scheduler.get_job(job_id):
        return
    scheduler.add_job(
        _poll_receiver,
        "interval",
        seconds=settings.poll_interval_sec,
        start_date=f"2000-01-01 00:00:{delay_sec:02d}",
        args=[name],
        id=job_id,
        max_instances=1,
        coalesce=True,
    )
    log.info("scheduler.poll_job_added", receiver=name)


def remove_poll_job(name: str) -> None:
    job_id = f"poll_{name}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        log.info("scheduler.poll_job_removed", receiver=name)


def _close_orphan_sessions() -> None:
    """Sessions with ended_at IS NULL from before a restart need closing —
    the in-memory state that would have closed them is gone. Confirmed ones
    keep their data with a best-effort end time; unconfirmed ones are
    discarded (we have no way to know they crossed min_watch_sec)."""
    from app.models import ViewingSession, Receiver
    db = SessionLocal()
    try:
        orphans = (
            db.query(ViewingSession)
            .filter(ViewingSession.ended_at == None)  # noqa: E711
            .all()
        )
        if not orphans:
            return
        receivers = {r.id: r for r in db.query(Receiver).all()}
        kept = discarded = 0
        min_dur = timedelta(seconds=settings.min_watch_sec)
        for s in orphans:
            if not s.confirmed:
                db.delete(s)
                discarded += 1
                continue
            rcv = receivers.get(s.receiver_id)
            candidates = [s.started_at + min_dur]
            if rcv and rcv.last_seen and rcv.last_seen > s.started_at:
                candidates.append(rcv.last_seen)
            s.ended_at = max(candidates)
            s.duration_sec = int((s.ended_at - s.started_at).total_seconds())
            kept += 1
        db.commit()
        log.info("poller.orphan_sweep", kept=kept, discarded=discarded)
    finally:
        db.close()


def start_scheduler() -> None:
    """Register all jobs and start the scheduler."""
    _close_orphan_sessions()
    from app.database import SessionLocal as _SL
    db = _SL()
    try:
        from app.services.receivers import get_receiver_configs
        for i, rcfg in enumerate(get_receiver_configs(db)):
            _add_poll_job(rcfg.name, delay_sec=i * 10)
    finally:
        db.close()

    scheduler.add_job(
        _refresh_all_epg,
        "interval",
        hours=1,
        id="refresh_epg",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _refresh_all_epg(full=True),
        "cron",
        hour=settings.epg_full_refresh_hour,
        minute=30,
        id="refresh_epg_full",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _refresh_all_channels,
        "interval",
        hours=24,
        id="refresh_channels",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _cleanup_epg,
        "cron",
        hour=4,
        minute=0,
        id="cleanup_epg",
    )
    scheduler.start()
    log.info("scheduler.started")
