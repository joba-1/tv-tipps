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


async def _find_receiver(preferred_name: str | None):
    """Return (rcfg, client) for the best available online receiver."""
    from app.enigma.client import EnigmaClient
    if preferred_name:
        rcfg = next((r for r in settings.receivers_by_priority if r.name == preferred_name), None)
        if rcfg:
            client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
            if await client.is_online():
                return rcfg, client
    for rcfg in settings.receivers_by_priority:
        client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
        if await client.is_online():
            return rcfg, client
    return None, None


@router.post("/api/remote/zap")
async def zap_to_channel(req: ZapRequest):
    from app.enigma.client import EnigmaClient
    from app.services.power import wake_receiver

    best_rcfg = None
    best_client = None

    # If a specific receiver was requested, try it first
    if req.receiver:
        preferred = next((r for r in settings.receivers_by_priority if r.name == req.receiver), None)
        if preferred:
            client = EnigmaClient(preferred.ip, mock=settings.mock_receivers)
            if await client.is_online():
                best_rcfg = preferred
                best_client = client

    if best_rcfg is None:
        for rcfg in settings.receivers_by_priority:
            client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
            if await client.is_online():
                best_rcfg = rcfg
                best_client = client
                break

    woke = False
    if best_rcfg is None:
        for rcfg in settings.receivers_by_priority:
            if await wake_receiver(rcfg):
                log.info("remote.waking_receiver", receiver=rcfg.name)
                client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
                for _ in range(6):
                    await asyncio.sleep(5)
                    if await client.is_online():
                        best_rcfg = rcfg
                        best_client = client
                        woke = True
                        break
                if best_rcfg:
                    break

    if best_rcfg is None or best_client is None:
        raise HTTPException(503, "No receiver available")

    ok = await best_client.zap(req.sref)
    log.info("remote.zap", receiver=best_rcfg.name, sref=req.sref, ok=ok)
    return {"ok": ok, "receiver_name": best_rcfg.name, "sref": req.sref, "woke": woke}


@router.post("/api/remote/key")
async def send_key(req: KeyRequest):
    """Send a single RC key press to the best available (or specified) receiver."""
    rcfg, client = await _find_receiver(req.receiver)
    if rcfg is None:
        raise HTTPException(503, "No receiver available")
    ok = await client.send_key(req.key)
    log.info("remote.key", receiver=rcfg.name, key=req.key, ok=ok)
    return {"ok": ok, "receiver_name": rcfg.name}


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
