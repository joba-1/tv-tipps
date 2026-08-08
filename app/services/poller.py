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
from app.services.power import wake_for_epg, sleep_receiver
from app.logging_setup import get_logger
from app.timezones import utcnow
from config import ReceiverConfig, settings

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

        from app.enigma.parser import parse_current, EnigmaParseError
        try:
            current = parse_current(raw_current)
        except EnigmaParseError as e:
            from app.services.forensics import dump_failure
            dump_failure("enigma", tag=f"{receiver_name}_current",
                         receiver=receiver_name, endpoint="getcurrent",
                         error=str(e), raw=raw_current)
            log.warning("poller.parse_failed", receiver=receiver_name, error=str(e))
            return
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
    # Score-backed recs read pre-computed per-event scores; the old per-context
    # warm is redundant. Per-event scoring happens at EPG ingest, not here.


async def _nightly_full_sweep() -> None:
    """The nightly full EPG sweep, preceded by waking every receiver flagged
    `epg_wake` and followed by powering those back down.

    Without this, a box that is normally switched off (Intertechno mains cut,
    deep standby) never contributes EPG at all — `_refresh_all_epg` only talks
    to receivers that answer HTTP, so the sweep just logged
    `epg.receiver_offline_skip` night after night and the cached EPG aged out.
    """
    woken = await _wake_epg_receivers()
    try:
        for rcfg in woken:
            await _prime_woken_receiver(rcfg)
        await _refresh_all_epg(full=True)
    finally:
        # Always hand the boxes back the way we found them, even if the sweep
        # raised — leaving a receiver powered up is the one outcome the user
        # would notice.
        for rcfg in woken:
            ok, reason = await sleep_receiver(rcfg)
            log.info("epg.wake_sleep_back", receiver=rcfg.name, ok=ok, reason=reason)


async def _wake_epg_receivers() -> list[ReceiverConfig]:
    """Power up the `epg_wake` receivers that are currently offline. Returns the
    ones we actually switched on, so only those get switched off again."""
    db = SessionLocal()
    try:
        from app.services.receivers import get_receiver_configs
        candidates = [r for r in get_receiver_configs(db) if r.epg_wake]
    finally:
        db.close()

    woken: list[ReceiverConfig] = []
    for rcfg in candidates:
        client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
        if await client.is_online():
            log.info("epg.wake_skip_online", receiver=rcfg.name)
            continue
        ok, reason = await wake_for_epg(rcfg)
        if ok:
            log.info("epg.wake_ok", receiver=rcfg.name, note=reason)
            woken.append(rcfg)
        else:
            log.warning("epg.wake_failed", receiver=rcfg.name, reason=reason)
    return woken


async def _prime_woken_receiver(rcfg: ReceiverConfig) -> None:
    """Zap-tour the transponders of the channels users actually see, so the box
    has EPG for more than the one transponder it booted on.

    Restricted to the visible channels (bouquet filters / favourites) because the
    tour costs `epg_wake_dwell_sec` per transponder — sweeping every transponder
    the box can tune would keep it powered up for hours.
    """
    from app.models import User
    from app.services.channels import get_channels_for_user
    from app.services.epg import prime_epg_cache

    db = SessionLocal()
    try:
        receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
        if not receiver:
            return
        visible: dict[int, Any] = {}
        for user in db.query(User).all():
            for ch in get_channels_for_user(user.id, db):
                visible[ch.id] = ch
        if not visible:
            log.info("epg.prime_skip", receiver=rcfg.name, reason="no visible channels")
            return
        client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
        await prime_epg_cache(receiver, client, list(visible.values()),
                              dwell_sec=settings.epg_wake_dwell_sec)
    except Exception as e:
        # A failed tour costs us data, not correctness — the sweep still runs on
        # whatever the box already had.
        log.warning("epg.prime_failed", receiver=rcfg.name, error=str(e))
    finally:
        db.close()


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


def _cleanup_old_sessions(retention_days: int, db: Session) -> int:
    """Viewing sessions past the retention window no longer feed the profile
    (30-day window) and only pin their EPG events against cleanup. Prune them
    first so those events become deletable by the EPG retention query."""
    from app.models import ViewingSession
    cutoff = utcnow() - timedelta(days=retention_days)
    n = (
        db.query(ViewingSession)
        .filter(ViewingSession.started_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    if n:
        log.info("poller.session_cleanup", deleted=n, retention_days=retention_days)
    return n


async def _cleanup_epg() -> None:
    db = SessionLocal()
    try:
        _cleanup_old_sessions(settings.session_retention_days, db)
        cleanup_old_events(settings.epg_retention_days, db)
    finally:
        db.close()


async def run_refresh(target: str) -> None:
    """Called from admin endpoint for on-demand refresh."""
    if target in ("channels", "all"):
        await _refresh_all_channels()
    if target == "epg_full":
        # Same treatment as the nightly job: an explicit full refresh should
        # reach the boxes that are powered down, too.
        await _nightly_full_sweep()
    elif target in ("epg", "all"):
        await _refresh_all_epg(full=False)


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
    # NOTE: the job callable must be the coroutine function itself — a sync
    # wrapper (lambda or def) runs in APScheduler's worker thread, where the
    # returned coroutine is discarded un-awaited and create_task has no loop.
    scheduler.add_job(
        _nightly_full_sweep,
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

    # Daily catch-all that re-rates any stale UserEventScore rows the debounced
    # like/dislike path missed. Runs after EPG cleanup so it sees the current
    # future-event set.
    from app.services.scoring import (
        daily_rerate_stale, ai_availability_watcher, bootstrap_unscored,
        scoring_worker,
    )

    scheduler.add_job(
        daily_rerate_stale,
        "cron",
        hour=4,
        minute=15,
        id="rerate_stale",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    log.info("scheduler.started")

    # Single consumer for the scoring ingest queue (EPG refresh paths enqueue).
    asyncio.create_task(scoring_worker())
    # Background loop: upgrade any 'rule'-sourced score rows to LLM scores the
    # moment Ollama becomes reachable again — without waiting for the cron.
    asyncio.create_task(ai_availability_watcher())
    # On startup: score any future EPG events we don't have rows for yet.
    asyncio.create_task(bootstrap_unscored())
