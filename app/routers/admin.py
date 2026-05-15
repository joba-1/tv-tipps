from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Receiver, Channel, EpgEvent
from app.schemas import AdminStatus, ReceiverStatus

router = APIRouter()


@router.get("/api/admin/status", response_model=AdminStatus)
async def admin_status(db: Session = Depends(get_db)):
    from app.enigma.client import EnigmaClient
    from config import settings

    receiver_statuses = []
    for rcfg in settings.receivers:
        receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
        client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
        online = await client.is_online()
        power_state = await client.get_power_state() if online else "unknown"

        # Most recent cached_at in epg_events for this receiver's channels
        epg_cached_at = None
        if receiver:
            from sqlalchemy import text
            row = db.execute(text(
                "SELECT MAX(e.cached_at) FROM epg_events e"
                " JOIN channels c ON e.channel_id = c.id"
                " JOIN channel_availability ca ON ca.channel_id = c.id"
                " WHERE ca.receiver_id = :rid"
            ), {"rid": receiver.id}).scalar()
            if row:
                epg_cached_at = str(row)

        receiver_statuses.append(ReceiverStatus(
            name=rcfg.name,
            ip=rcfg.ip,
            online=online,
            power_state=power_state,
            last_seen=receiver.last_seen.isoformat() if receiver and receiver.last_seen else None,
            epg_cached_at=epg_cached_at,
        ))

    return AdminStatus(
        receivers=receiver_statuses,
        db_channel_count=db.query(Channel).count(),
        db_epg_event_count=db.query(EpgEvent).count(),
    )


@router.post("/api/admin/refresh")
async def admin_refresh(target: str = "all", background_tasks = None):
    """Trigger immediate refresh. target: channels | epg | epg_full | all"""
    from fastapi import BackgroundTasks
    from app.services.poller import run_refresh
    # Run directly (fast for now/next; use background task for epg_full)
    await run_refresh(target)
    return {"ok": True, "target": target}


@router.post("/api/admin/power")
async def admin_power(receiver: str, action: str):
    """Manual power control. receiver: box15|box17, action: wake|sleep|on|off"""
    from app.services.power import wake_box15, sleep_box15, power_on_box17, power_off_box17
    if receiver == "box15":
        if action == "wake":
            ok = await wake_box15()
        elif action == "sleep":
            ok = await sleep_box15()
        else:
            return {"ok": False, "error": "box15 supports: wake, sleep"}
    elif receiver == "box17":
        if action == "on":
            ok = await power_on_box17()
        elif action == "off":
            ok = await power_off_box17()
        else:
            return {"ok": False, "error": "box17 supports: on, off"}
    else:
        return {"ok": False, "error": "unknown receiver"}
    return {"ok": ok, "receiver": receiver, "action": action}
