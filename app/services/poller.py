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

        rcfg = next((r for r in settings.receivers if r.name == receiver_name), None)
        if not rcfg:
            return

        client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
        state = _get_state(receiver_name)
        now = datetime.utcnow()

        # Check power state
        power = await client.get_power_state()
        state["power_state"] = power
        receiver.power_state = power

        if power != "on":
            if state["session_id"] and state["confirmed"]:
                _close_session(state, now, db)
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
    session = db.query(ViewingSession).get(state["session_id"])
    if not session:
        return
    session.confirmed = True

    # Attribute to default user for this receiver
    from app.models import User
    if receiver.default_user:
        user = db.query(User).filter_by(slug=receiver.default_user).first()
        if user:
            session.user_id = user.id
            session.confidence = 0.6
            session.attribution_method = "default"

    # Link EPG event
    channel = db.query(Channel).get(session.channel_id)
    if channel:
        epg_event = (
            db.query(EpgEvent)
            .filter(
                EpgEvent.channel_id == channel.id,
                EpgEvent.start_time <= session.started_at,
                EpgEvent.end_time > session.started_at,
            )
            .first()
        )
        if epg_event:
            session.epg_event_id = epg_event.id

    state["confirmed"] = True
    log.info("poller.session_confirmed", session_id=state["session_id"],
             receiver=receiver.name, user=receiver.default_user)


def _close_session(state: dict, ended_at: datetime, db: Session) -> None:
    from app.models import ViewingSession
    session = db.query(ViewingSession).get(state["session_id"])
    if session:
        session.ended_at = ended_at
        if session.started_at:
            session.duration_sec = int((ended_at - session.started_at).total_seconds())
    state["session_id"] = None
    state["session_start"] = None
    state["confirmed"] = False


async def _find_best_receiver(db) -> tuple | None:
    """Return (receiver, client) in priority order; prefer has_genre=True for full EPG."""
    for rcfg in settings.receivers_by_priority:
        receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
        if not receiver:
            continue
        client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
        if await client.is_online():
            return receiver, client
    return None


async def _refresh_all_epg(full: bool = False) -> None:
    """Refresh EPG from the best available receiver.
    Tries all online receivers for now/next; uses best one for full service fetch.
    """
    db = SessionLocal()
    try:
        best = None
        for rcfg in settings.receivers_by_priority:
            receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
            if not receiver:
                continue
            client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
            if await client.is_online():
                await refresh_now_next(receiver, client, db)
                # Prefer receiver with genre data as source for full EPG fetch
                if best is None or rcfg.has_genre:
                    best = (receiver, client)
            else:
                log.info("epg.receiver_offline_skip", receiver=rcfg.name)

        if full and best:
            receiver, client = best
            from app.models import Channel, BouquetChannel, Bouquet
            from app.services.epg import refresh_epg_service
            bouquet_ids = [b.id for b in db.query(Bouquet).filter_by(receiver_id=receiver.id).all()]
            channel_ids = {
                bc.channel_id for bc in
                db.query(BouquetChannel).filter(BouquetChannel.bouquet_id.in_(bouquet_ids)).all()
            }
            channels = db.query(Channel).filter(Channel.id.in_(channel_ids)).all()
            log.info("epg.full_refresh_start", receiver=receiver.name, channels=len(channels))
            for ch in channels:
                await refresh_epg_service(ch, client, db, hours=24)
            log.info("epg.full_refresh_done", receiver=receiver.name)
    except Exception as e:
        log.error("epg.refresh_error", error=str(e))
    finally:
        db.close()


async def _refresh_all_channels() -> None:
    db = SessionLocal()
    try:
        for rcfg in settings.receivers:
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


def start_scheduler() -> None:
    """Register all jobs and start the scheduler."""
    for i, rcfg in enumerate(settings.receivers):
        delay = i * 10  # stagger polls between boxes
        scheduler.add_job(
            _poll_receiver,
            "interval",
            seconds=settings.poll_interval_sec,
            start_date=f"2000-01-01 00:00:{delay:02d}",
            args=[rcfg.name],
            id=f"poll_{rcfg.name}",
            max_instances=1,
            coalesce=True,
        )

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
