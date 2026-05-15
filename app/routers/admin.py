from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Receiver, Channel, EpgEvent
from app.schemas import AdminStatus, ReceiverStatus

router = APIRouter()


@router.get("/api/receivers")
async def list_receivers(db: Session = Depends(get_db)):
    """Return configured receivers with current power state and capability flags."""
    from app.enigma.client import EnigmaClient
    from config import settings

    result = []
    for rcfg in settings.receivers_by_priority:
        receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
        client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
        online = await client.is_online()
        result.append({
            "name": rcfg.name,
            "ip": rcfg.ip,
            "priority": rcfg.priority,
            "has_genre": rcfg.has_genre,
            "power_method": rcfg.power_method,
            "online": online,
            "power_state": receiver.power_state if receiver else "unknown",
            "last_seen": receiver.last_seen.isoformat() if receiver and receiver.last_seen else None,
            "wol_mac": rcfg.wol_mac,
        })
    return result


@router.get("/api/users")
def list_users(db: Session = Depends(get_db)):
    from app.models import User
    return [{"slug": u.slug, "name": u.name} for u in db.query(User).all()]


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
            power_method=rcfg.power_method,
            has_genre=rcfg.has_genre,
            priority=rcfg.priority,
        ))

    return AdminStatus(
        receivers=receiver_statuses,
        db_channel_count=db.query(Channel).count(),
        db_epg_event_count=db.query(EpgEvent).count(),
    )


@router.post("/api/admin/refresh")
async def admin_refresh(target: str = "all"):
    """Trigger immediate refresh. target: channels | epg | epg_full | all"""
    from app.services.poller import run_refresh
    await run_refresh(target)
    return {"ok": True, "target": target}


@router.post("/api/admin/power")
async def admin_power(receiver: str, action: str):
    """Manual power control. action: wake | sleep"""
    from config import settings
    from app.services.power import wake_receiver, sleep_receiver

    rcfg = next((r for r in settings.receivers if r.name == receiver), None)
    if not rcfg:
        known = [r.name for r in settings.receivers]
        return {"ok": False, "error": f"unknown receiver '{receiver}', known: {known}"}

    if action == "wake":
        ok = await wake_receiver(rcfg)
    elif action == "sleep":
        ok = await sleep_receiver(rcfg)
    else:
        return {"ok": False, "error": "action must be 'wake' or 'sleep'"}

    return {"ok": ok, "receiver": receiver, "action": action, "power_method": rcfg.power_method}
