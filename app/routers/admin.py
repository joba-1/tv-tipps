from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Receiver, Channel, EpgEvent, User
from app.schemas import AdminStatus, ReceiverStatus
from app.services.receivers import get_receiver_configs, _to_rcfg

router = APIRouter()


def _infer_power_state(online: bool, rcfg, receiver) -> str:
    if online:
        return receiver.power_state if receiver else "unknown"
    if rcfg.power_method == "intertechno":
        return "off"
    if rcfg.power_method == "wol":
        return "standby"
    return receiver.power_state if receiver else "unknown"


# ── Receivers ─────────────────────────────────────────────────────────────────

class ReceiverCreateRequest(BaseModel):
    name: str
    ip: str
    location: str = ""
    priority: int = 99
    power_method: str = "none"
    wol_mac: str | None = None
    has_genre: bool = False
    default_user: str | None = None


@router.get("/api/receivers")
async def list_receivers(db: Session = Depends(get_db)):
    from app.enigma.client import EnigmaClient
    from config import settings
    result = []
    for rcfg in get_receiver_configs(db):
        receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
        client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
        online = await client.is_online()
        power_state = _infer_power_state(online, rcfg, receiver)
        result.append({
            "name": rcfg.name,
            "ip": rcfg.ip,
            "priority": rcfg.priority,
            "has_genre": rcfg.has_genre,
            "power_method": rcfg.power_method,
            "location": rcfg.location,
            "online": online,
            "power_state": power_state,
            "last_seen": receiver.last_seen.isoformat() if receiver and receiver.last_seen else None,
            "wol_mac": rcfg.wol_mac,
        })
    return result


@router.post("/api/admin/receivers")
def create_receiver(req: ReceiverCreateRequest, db: Session = Depends(get_db)):
    if db.query(Receiver).filter_by(name=req.name).first():
        raise HTTPException(409, f"Receiver '{req.name}' already exists")
    r = Receiver(
        name=req.name, ip=req.ip, location=req.location, priority=req.priority,
        power_method=req.power_method, wol_mac=req.wol_mac or None,
        has_genre=req.has_genre, default_user=req.default_user,
        power_state="unknown",
    )
    db.add(r)
    db.commit()
    # Add polling job for the new receiver
    from app.services.poller import _add_poll_job
    _add_poll_job(req.name)
    return {"ok": True, "name": req.name}


@router.delete("/api/admin/receivers/{name}")
def delete_receiver(name: str, db: Session = Depends(get_db)):
    r = db.query(Receiver).filter_by(name=name).first()
    if not r:
        raise HTTPException(404, f"Receiver '{name}' not found")
    from app.services.poller import remove_poll_job
    remove_poll_job(name)
    db.delete(r)
    db.commit()
    return {"ok": True, "name": name}


# ── Users ─────────────────────────────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    slug: str
    name: str


@router.get("/api/users")
def list_users(db: Session = Depends(get_db)):
    return [{"slug": u.slug, "name": u.name} for u in db.query(User).order_by(User.name).all()]


@router.post("/api/admin/users")
def create_user(req: UserCreateRequest, db: Session = Depends(get_db)):
    slug = req.slug.strip().lower()
    if not slug:
        raise HTTPException(400, "slug must not be empty")
    if db.query(User).filter_by(slug=slug).first():
        raise HTTPException(409, f"User '{slug}' already exists")
    from app.timezones import utcnow
    db.add(User(slug=slug, name=req.name.strip(), created_at=utcnow()))
    db.commit()
    return {"ok": True, "slug": slug}


@router.delete("/api/admin/users/{slug}")
def delete_user(slug: str, db: Session = Depends(get_db)):
    u = db.query(User).filter_by(slug=slug).first()
    if not u:
        raise HTTPException(404, f"User '{slug}' not found")
    db.delete(u)
    db.commit()
    return {"ok": True, "slug": slug}


# ── Admin status ──────────────────────────────────────────────────────────────

@router.get("/api/admin/status", response_model=AdminStatus)
async def admin_status(db: Session = Depends(get_db)):
    from app.enigma.client import EnigmaClient
    from config import settings

    receiver_statuses = []
    for rcfg in get_receiver_configs(db):
        receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
        client = EnigmaClient(rcfg.ip, mock=settings.mock_receivers)
        online = await client.is_online()
        if online:
            power_state = await client.get_power_state()
            if receiver:
                receiver.power_state = power_state
        else:
            power_state = _infer_power_state(False, rcfg, receiver)

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
            name=rcfg.name, ip=rcfg.ip, online=online, power_state=power_state,
            last_seen=receiver.last_seen.isoformat() if receiver and receiver.last_seen else None,
            epg_cached_at=epg_cached_at, power_method=rcfg.power_method,
            has_genre=rcfg.has_genre, priority=rcfg.priority, location=rcfg.location,
        ))
    db.commit()

    return AdminStatus(
        receivers=receiver_statuses,
        db_channel_count=db.query(Channel).count(),
        db_epg_event_count=db.query(EpgEvent).count(),
    )


@router.post("/api/admin/refresh")
async def admin_refresh(target: str = "all"):
    from app.services.poller import run_refresh
    await run_refresh(target)
    return {"ok": True, "target": target}


# ── User preferences ──────────────────────────────────────────────────────────

class UserPreferencesRequest(BaseModel):
    preferences: str


@router.get("/api/admin/user-preferences")
def get_user_preferences(user: str, db: Session = Depends(get_db)):
    import json
    from app.models import UserProfile
    u = db.query(User).filter_by(slug=user).first()
    if not u:
        return {"ok": False, "error": f"unknown user '{user}'"}
    profile = db.get(UserProfile, u.id)
    prefs = ""
    if profile:
        try:
            prefs = json.loads(profile.summary_json).get("stated_preferences") or ""
        except Exception:
            pass
    return {"ok": True, "user": user, "preferences": prefs}


@router.post("/api/admin/user-preferences")
def set_user_preferences(user: str, req: UserPreferencesRequest, db: Session = Depends(get_db)):
    from app.models import RecommendationCache
    from app.services.profile import set_stated_preferences
    u = db.query(User).filter_by(slug=user).first()
    if not u:
        return {"ok": False, "error": f"unknown user '{user}'"}
    set_stated_preferences(u.id, req.preferences, db)
    db.query(RecommendationCache).filter_by(user_id=u.id).delete()
    db.commit()
    return {"ok": True, "user": user}


# ── Power control ─────────────────────────────────────────────────────────────

@router.post("/api/admin/power")
async def admin_power(receiver: str, action: str, db: Session = Depends(get_db)):
    from app.services.power import wake_receiver, sleep_receiver
    rcfg = next((r for r in get_receiver_configs(db) if r.name == receiver), None)
    if not rcfg:
        raise HTTPException(404, f"Receiver '{receiver}' not found")
    if action == "wake":
        ok = await wake_receiver(rcfg)
    elif action == "sleep":
        ok = await sleep_receiver(rcfg)
    else:
        raise HTTPException(400, "action must be 'wake' or 'sleep'")
    return {"ok": ok, "receiver": receiver, "action": action, "power_method": rcfg.power_method}
