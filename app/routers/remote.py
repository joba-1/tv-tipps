"""Remote control: zap to channel with automatic receiver wake if needed."""
from __future__ import annotations
import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.logging_setup import get_logger
from config import settings

log = get_logger(__name__)
router = APIRouter()


class ZapRequest(BaseModel):
    sref: str


@router.post("/api/remote/zap")
async def zap_to_channel(req: ZapRequest):
    from app.enigma.client import EnigmaClient
    from app.services.power import wake_receiver

    best_rcfg = None
    best_client = None

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
