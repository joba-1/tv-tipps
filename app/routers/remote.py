"""Remote control: zap to channel with automatic receiver wake if needed."""
from __future__ import annotations
import asyncio
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from app.logging_setup import get_logger
from config import settings

log = get_logger(__name__)
router = APIRouter()


class ZapRequest(BaseModel):
    sref: str
    receiver: str | None = None  # preferred receiver name; None = best available


class KeyRequest(BaseModel):
    key: str
    receiver: str | None = None


class TimerRequest(BaseModel):
    sref: str
    start_time: str   # naive UTC ISO (matches our EPG output)
    end_time: str     # naive UTC ISO
    title: str
    short_desc: str | None = None
    event_id: int | None = None   # EPG eit — helps OWIF bind the timer reliably
    receiver: str | None = None


class TimerRemoveRequest(BaseModel):
    sref: str
    begin: int        # epoch seconds — must match the timer's begin exactly
    end: int          # epoch seconds — must match the timer's end exactly
    receiver: str | None = None


async def _find_receiver(preferred_name: str | None):
    """Return (rcfg, client) for the requested receiver.

    If a name is given, return that receiver regardless of online state
    (caller decides how to handle offline). If no name given, return the
    first online receiver by priority order.
    """
    from app.enigma.client import EnigmaClient
    from app.database import SessionLocal
    from app.services.receivers import get_receiver_configs, get_receiver_config
    db = SessionLocal()
    try:
        if preferred_name:
            rcfg = get_receiver_config(preferred_name, db)
            if rcfg:
                return rcfg, EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
            return None, None
        for rcfg in get_receiver_configs(db):
            client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
            if await client.is_online():
                return rcfg, client
    finally:
        db.close()
    return None, None


@router.post("/api/remote/zap")
async def zap_to_channel(req: ZapRequest):
    from app.enigma.client import EnigmaClient
    from app.services.power import wake_receiver

    rcfg, client = await _find_receiver(req.receiver)
    woke = False

    if rcfg is None or client is None:
        raise HTTPException(503, "No receiver available")

    # Wake from deep standby (unreachable) → send WOL/intertechno, poll until online
    if not await client.is_online():
        if not await wake_receiver(rcfg):
            raise HTTPException(503, f"Could not wake {rcfg.name}")
        log.info("remote.waking_receiver", receiver=rcfg.name)
        for _ in range(6):
            await asyncio.sleep(5)
            if await client.is_online():
                woke = True
                break
        if not woke:
            raise HTTPException(503, f"{rcfg.name} did not come online after wake")

    # Wake from light standby before zapping
    if not woke and await client.get_power_state() == "standby":
        await client.send_key("power")
        await asyncio.sleep(3)
        woke = True

    ok = await client.zap(req.sref)
    log.info("remote.zap", receiver=rcfg.name, sref=req.sref, ok=ok)
    return {"ok": ok, "receiver_name": rcfg.name, "receiver_location": rcfg.location or rcfg.name, "sref": req.sref, "woke": woke}


@router.post("/api/remote/key")
async def send_key(req: KeyRequest):
    """Send a single RC key press to the best available (or specified) receiver."""
    rcfg, client = await _find_receiver(req.receiver)
    if rcfg is None:
        raise HTTPException(503, "No receiver available")
    ok = await client.send_key(req.key)
    log.info("remote.key", receiver=rcfg.name, key=req.key, ok=ok)
    return {"ok": ok, "receiver_name": rcfg.name}


@router.get("/api/remote/timers")
async def list_timers(receiver: str | None = None):
    """Return active recording timers from the receiver. Empty list if offline."""
    rcfg, client = await _find_receiver(receiver)
    if rcfg is None or client is None:
        return {"timers": [], "receiver_name": None}
    if not await client.is_online():
        return {"timers": [], "receiver_name": rcfg.name}
    timers = await client.list_timers()
    return {"timers": timers, "receiver_name": rcfg.name}


@router.post("/api/remote/timer/remove")
async def remove_timer(req: TimerRemoveRequest):
    rcfg, client = await _find_receiver(req.receiver)
    if rcfg is None or client is None:
        raise HTTPException(503, "No receiver available")
    if not await client.is_online():
        raise HTTPException(503, f"{rcfg.name} is offline — wake it first")
    ok = await client.delete_timer(req.sref, req.begin, req.end)
    log.info("remote.timer_remove", receiver=rcfg.name, sref=req.sref,
             begin=req.begin, end=req.end, ok=ok)
    return {
        "ok": ok,
        "receiver_name": rcfg.name,
        "receiver_location": rcfg.location or rcfg.name,
    }


@router.post("/api/remote/record")
async def add_record_timer(req: TimerRequest):
    """Program a recording timer on the receiver for the given event."""
    from datetime import datetime, timezone
    rcfg, client = await _find_receiver(req.receiver)
    if rcfg is None or client is None:
        raise HTTPException(503, "No receiver available")
    if not await client.is_online():
        raise HTTPException(503, f"{rcfg.name} is offline — wake it first")

    try:
        start = datetime.fromisoformat(req.start_time)
        end = datetime.fromisoformat(req.end_time)
    except ValueError:
        raise HTTPException(400, "Invalid start_time/end_time")

    # Naive UTC → epoch; pad 2 min before, 5 min after (broadcast slip).
    begin_ts = int(start.replace(tzinfo=timezone.utc).timestamp()) - 120
    end_ts = int(end.replace(tzinfo=timezone.utc).timestamp()) + 300

    resp = await client.add_timer(
        sref=req.sref, begin=begin_ts, end=end_ts,
        name=req.title, description=req.short_desc or "",
        eit=req.event_id,
    )
    ok = bool(resp and resp.get("result"))
    msg = (resp or {}).get("message", "") or ""
    log.info("remote.record",
             receiver=rcfg.name, sref=req.sref, title=req.title,
             eit=req.event_id, ok=ok, owif_message=msg)
    return {
        "ok": ok,
        "message": msg,
        "receiver_name": rcfg.name,
        "receiver_location": rcfg.location or rcfg.name,
    }


@router.get("/api/remote/screenshot")
async def get_screenshot(receiver: str | None = None):
    """Proxy a JPEG screenshot from the best available (or specified) receiver."""
    rcfg, client = await _find_receiver(receiver)
    if rcfg is None:
        raise HTTPException(503, "No receiver available")
    data = await client.screenshot()
    if data is None:
        raise HTTPException(503, "Screenshot unavailable")
    return Response(content=data, media_type="image/jpeg")
