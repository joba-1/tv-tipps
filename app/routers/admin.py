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
    intertechno_family: str = ""
    intertechno_device: int = 1
    intertechno_url: str = ""
    has_genre: bool = False
    default_user: str | None = None


@router.get("/api/receivers")
async def list_receivers(db: Session = Depends(get_db)):
    # No live probe — read the cached power_state the 45s poller writes. This
    # keeps initial page load fast even when receivers are unreachable.
    # The frontend refresh interval (30s) + poller (45s) keep the dot fresh.
    from app.timezones import utcnow
    rcfgs = get_receiver_configs(db)
    now = utcnow()
    result = []
    for rcfg in rcfgs:
        receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
        cached_state = receiver.power_state if receiver else "unknown"
        online = cached_state in ("on", "standby")
        # Same inference probe_receivers applies on the response: intertechno
        # boxes map to "off", WOL boxes to "standby" when unreachable, instead
        # of the ambiguous "unknown" the HTTP probe records in the DB.
        power_state = cached_state if online else _infer_power_state(False, rcfg, receiver)
        # "probing" → DB state is too old to trust AND we have no reliable
        # inference to fall back to. Intertechno→"off" and WOL→"standby" are
        # definitive enough to skip pulsing.
        probing = (
            receiver is None
            or receiver.last_seen is None
            or (now - receiver.last_seen).total_seconds() > 120
        ) and online is False and power_state == "unknown"
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
            "intertechno_family": rcfg.intertechno_family,
            "intertechno_device": rcfg.intertechno_device,
            "intertechno_url": rcfg.intertechno_url,
            "default_user": rcfg.default_user or None,
            "probing": probing,
        })
    return result


@router.get("/api/receivers/probe")
async def probe_receivers(db: Session = Depends(get_db)):
    """Live probe all receivers in parallel, update DB cache, return fresh
    state. Used by the frontend right after the cheap /api/receivers call so
    the dots flip from probing → real state within ~1.5 s of page load."""
    import asyncio
    from app.enigma.client import EnigmaClient
    from app.timezones import utcnow
    from config import settings

    rcfgs = get_receiver_configs(db)
    clients = [EnigmaClient(r.ip, mock=settings.mock_receivers) for r in rcfgs]

    async def _probe(client: EnigmaClient) -> tuple[bool, str]:
        if not await client.is_online():
            return False, "unknown"
        return True, await client.get_power_state()

    results = await asyncio.gather(*(_probe(c) for c in clients))

    now = utcnow()
    out = []
    for rcfg, (online, power_state) in zip(rcfgs, results):
        receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
        if receiver:
            receiver.power_state = power_state
            if online:
                receiver.last_seen = now
        # When the live probe says "off"/"standby" but the receiver is
        # unreachable, fall back to the same inference list_receivers uses.
        if not online:
            power_state = _infer_power_state(False, rcfg, receiver)
        out.append({
            "name": rcfg.name,
            "ip": rcfg.ip,
            "priority": rcfg.priority,
            "has_genre": rcfg.has_genre,
            "power_method": rcfg.power_method,
            "location": rcfg.location,
            "online": online or power_state in ("on", "standby"),
            "power_state": power_state,
            "last_seen": receiver.last_seen.isoformat() if receiver and receiver.last_seen else None,
            "wol_mac": rcfg.wol_mac,
            "intertechno_family": rcfg.intertechno_family,
            "intertechno_device": rcfg.intertechno_device,
            "intertechno_url": rcfg.intertechno_url,
            "default_user": rcfg.default_user or None,
            "probing": False,
        })
    db.commit()
    return out


@router.post("/api/admin/receivers")
def create_receiver(req: ReceiverCreateRequest, db: Session = Depends(get_db)):
    if db.query(Receiver).filter_by(name=req.name).first():
        raise HTTPException(409, f"Receiver '{req.name}' already exists")
    r = Receiver(
        name=req.name, ip=req.ip, location=req.location, priority=req.priority,
        power_method=req.power_method, wol_mac=req.wol_mac or None,
        intertechno_family=req.intertechno_family, intertechno_device=req.intertechno_device,
        intertechno_url=req.intertechno_url,
        has_genre=req.has_genre, default_user=req.default_user,
        power_state="unknown",
    )
    db.add(r)
    db.commit()
    # Add polling job for the new receiver
    from app.services.poller import _add_poll_job
    _add_poll_job(req.name)
    return {"ok": True, "name": req.name}


class ReceiverPatchRequest(BaseModel):
    # All optional — None = "leave as-is". Empty string clears string fields.
    ip: str | None = None
    location: str | None = None
    priority: int | None = None
    power_method: str | None = None
    wol_mac: str | None = None
    intertechno_family: str | None = None
    intertechno_device: int | None = None
    intertechno_url: str | None = None
    has_genre: bool | None = None
    default_user: str | None = None


@router.patch("/api/admin/receivers/{name}")
def update_receiver(name: str, req: ReceiverPatchRequest, db: Session = Depends(get_db)):
    r = db.query(Receiver).filter_by(name=name).first()
    if not r:
        raise HTTPException(404, f"Receiver '{name}' not found")

    if req.ip is not None:
        ip = req.ip.strip()
        if not ip:
            raise HTTPException(400, "ip must not be empty")
        r.ip = ip
    if req.location is not None:
        r.location = req.location.strip()
    if req.priority is not None:
        if not 1 <= req.priority <= 99:
            raise HTTPException(400, "priority must be 1..99")
        r.priority = req.priority
    if req.power_method is not None:
        pm = req.power_method.strip().lower()
        if pm not in ("none", "wol", "intertechno"):
            raise HTTPException(400, "power_method must be none|wol|intertechno")
        r.power_method = pm
    if req.wol_mac is not None:
        r.wol_mac = req.wol_mac.strip() or None
    if req.intertechno_family is not None:
        fam = req.intertechno_family.strip()
        if fam and fam.lower() not in ("a", "b", "c", "d"):
            raise HTTPException(400, "intertechno_family must be A-D")
        r.intertechno_family = fam.upper()
    if req.intertechno_device is not None:
        if req.intertechno_device not in (1, 2, 3):
            raise HTTPException(400, "intertechno_device must be 1-3")
        r.intertechno_device = req.intertechno_device
    if req.intertechno_url is not None:
        r.intertechno_url = req.intertechno_url.strip()
    if req.has_genre is not None:
        r.has_genre = bool(req.has_genre)
    if req.default_user is not None:
        slug = req.default_user.strip() or None
        if slug and not db.query(User).filter_by(slug=slug).first():
            raise HTTPException(400, f"Unknown user '{slug}'")
        r.default_user = slug or ""

    db.commit()
    return {
        "ok": True, "name": r.name, "ip": r.ip, "location": r.location,
        "priority": r.priority, "power_method": r.power_method,
        "wol_mac": r.wol_mac, "intertechno_family": r.intertechno_family,
        "intertechno_device": r.intertechno_device,
        "intertechno_url": r.intertechno_url,
        "has_genre": r.has_genre,
        "default_user": r.default_user or None,
    }


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


# ── Ollama usage stats ────────────────────────────────────────────────────────

@router.get("/api/admin/ollama-stats")
async def ollama_stats():
    from app.services.ollama import get_stats
    return get_stats()


@router.post("/api/admin/ollama-stats/reset")
async def ollama_stats_reset():
    from app.services.ollama import reset_stats
    reset_stats()
    return {"ok": True}


# ── Admin status ──────────────────────────────────────────────────────────────

@router.get("/api/admin/status", response_model=AdminStatus)
async def admin_status(db: Session = Depends(get_db)):
    import asyncio
    from app.enigma.client import EnigmaClient
    from config import settings

    rcfgs = get_receiver_configs(db)
    clients = [EnigmaClient(r.ip, mock=settings.mock_receivers) for r in rcfgs]
    online_states = await asyncio.gather(*(c.is_online() for c in clients))

    receiver_statuses = []
    for rcfg, client, online in zip(rcfgs, clients, online_states):
        receiver = db.query(Receiver).filter_by(name=rcfg.name).first()
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
            default_user=rcfg.default_user or None,
            wol_mac=rcfg.wol_mac, intertechno_family=rcfg.intertechno_family,
            intertechno_device=rcfg.intertechno_device, intertechno_url=rcfg.intertechno_url,
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
async def set_user_preferences(user: str, req: UserPreferencesRequest, db: Session = Depends(get_db)):
    from app.services.profile import set_stated_preferences
    from app.services.scoring import mark_user_llm_rows_stale, schedule_rerate
    u = db.query(User).filter_by(slug=user).first()
    if not u:
        return {"ok": False, "error": f"unknown user '{user}'"}
    set_stated_preferences(u.id, req.preferences, db)
    # Preferences just changed → every existing LLM/rule score for this user
    # reflects the old profile. Mark stale + queue a debounced re-rate so the
    # new preferences propagate without waiting for the nightly cron.
    mark_user_llm_rows_stale(u.id, except_event_id=None, db=db)
    schedule_rerate(u.id)
    return {"ok": True, "user": user}


# ── User activity (sessions + likes/dislikes) ────────────────────────────────

@router.get("/api/admin/user-activity")
def get_user_activity(user: str, limit: int = 50, db: Session = Depends(get_db)):
    """Recent confirmed viewing sessions + all likes/dislikes for one user.
    Both shape the recommendation match score, so admins can prune outliers."""
    from app.models import ViewingSession, UserLike, EpgEvent, Channel
    u = db.query(User).filter_by(slug=user).first()
    if not u:
        raise HTTPException(404, f"unknown user '{user}'")

    rows = (
        db.query(ViewingSession, EpgEvent, Channel)
        .outerjoin(EpgEvent, ViewingSession.epg_event_id == EpgEvent.id)
        .outerjoin(Channel, ViewingSession.channel_id == Channel.id)
        .filter(ViewingSession.user_id == u.id, ViewingSession.confirmed == True)  # noqa: E712
        .order_by(ViewingSession.started_at.desc())
        .limit(limit)
        .all()
    )
    sessions = [{
        "id": s.id,
        "title": (ev.title if ev else None) or "(unknown)",
        "channel_name": ch.name if ch else None,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "duration_sec": s.duration_sec,
        "source": s.source,
    } for s, ev, ch in rows]

    likes = [{
        "id": l.id,
        "title": l.title,
        "channel_name": l.channel_name,
        "genre": l.genre,
        "sentiment": l.sentiment,
        "created_at": l.created_at.isoformat() if l.created_at else None,
    } for l in db.query(UserLike)
                .filter_by(user_id=u.id)
                .order_by(UserLike.created_at.desc())
                .all()]

    return {"user": user, "sessions": sessions, "likes": likes}


@router.get("/api/admin/active-viewers")
def active_viewers():
    """Per-receiver browser user that drove the most recent remote action
    (used for high-confidence session attribution). Expires after 4h."""
    from app.services.active_viewer import snapshot
    return snapshot()


@router.get("/api/admin/unattributed-sessions")
def list_unattributed_sessions(limit: int = 100, db: Session = Depends(get_db)):
    """Confirmed viewing sessions without a user, restricted to ones that
    are actually useful as a viewing signal: a resolved EPG event AND at
    least half its duration watched. Visible to the recommender only after
    an admin attributes them."""
    from app.models import ViewingSession, EpgEvent, Channel, Receiver as RcvModel
    rows = (
        db.query(ViewingSession, EpgEvent, Channel, RcvModel)
        .join(EpgEvent, ViewingSession.epg_event_id == EpgEvent.id)
        .outerjoin(Channel, ViewingSession.channel_id == Channel.id)
        .outerjoin(RcvModel, ViewingSession.receiver_id == RcvModel.id)
        .filter(
            ViewingSession.user_id == None,  # noqa: E711
            ViewingSession.confirmed == True,  # noqa: E712
            EpgEvent.title != None,  # noqa: E711
            EpgEvent.title != "",
        )
        .order_by(ViewingSession.started_at.desc())
        .limit(limit * 2)  # filter further client-side after %-watched check
        .all()
    )
    out = []
    for s, ev, ch, rc in rows:
        ev_dur = int((ev.end_time - ev.start_time).total_seconds()) if ev and ev.end_time and ev.start_time else 0
        watched = s.duration_sec or 0
        if ev_dur <= 0 or watched * 2 < ev_dur:
            continue
        out.append({
            "id": s.id,
            "title": ev.title,
            "channel_name": ch.name if ch else None,
            "receiver_name": rc.name if rc else None,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "duration_sec": watched,
            "event_duration_sec": ev_dur,
            "watched_pct": min(999, round(100.0 * watched / ev_dur, 0)),
            "source": s.source,
        })
        if len(out) >= limit:
            break
    return {"sessions": out}


class SessionAssignRequest(BaseModel):
    user: str  # slug, or "" to unassign


@router.post("/api/admin/sessions/{session_id}/assign")
async def assign_session(session_id: int, req: SessionAssignRequest, db: Session = Depends(get_db)):
    from app.models import ViewingSession
    s = db.get(ViewingSession, session_id)
    if not s:
        raise HTTPException(404, "session not found")
    slug = req.user.strip()
    old_uid = s.user_id
    if slug:
        u = db.query(User).filter_by(slug=slug).first()
        if not u:
            raise HTTPException(400, f"Unknown user '{slug}'")
        s.user_id = u.id
        s.confidence = 1.0
        s.attribution_method = "admin_manual"
    else:
        s.user_id = None
        s.attribution_method = None
    # Invalidate scores for both old and new owner so the next re-rate re-ranks.
    from app.services.scoring import mark_user_llm_rows_stale, schedule_rerate
    for uid in {old_uid, s.user_id} - {None}:
        mark_user_llm_rows_stale(uid, except_event_id=None, db=db)
        schedule_rerate(uid)
    db.commit()
    return {"ok": True, "id": session_id, "user_id": s.user_id}


@router.delete("/api/admin/sessions/{session_id}")
def delete_session(session_id: int, db: Session = Depends(get_db)):
    from app.models import ViewingSession
    s = db.get(ViewingSession, session_id)
    if not s:
        raise HTTPException(404, "session not found")
    db.delete(s)
    db.commit()
    return {"ok": True, "id": session_id}


@router.delete("/api/admin/likes/{like_id}")
def delete_like(like_id: int, db: Session = Depends(get_db)):
    from app.models import UserLike
    l = db.get(UserLike, like_id)
    if not l:
        raise HTTPException(404, "like not found")
    db.delete(l)
    db.commit()
    return {"ok": True, "id": like_id}


# ── Power control ─────────────────────────────────────────────────────────────

@router.post("/api/admin/power")
async def admin_power(receiver: str, action: str, db: Session = Depends(get_db)):
    from app.services.power import wake_receiver, sleep_receiver, openwebif_standby
    rcfg = next((r for r in get_receiver_configs(db) if r.name == receiver), None)
    if not rcfg:
        raise HTTPException(404, f"Receiver '{receiver}' not found")
    if action == "wake":
        ok, reason = await wake_receiver(rcfg)
    elif action == "sleep":
        ok, reason = await sleep_receiver(rcfg)
    elif action == "standby":
        # Light standby via OpenWebif only — leaves mains on. Useful for
        # Intertechno boxes where 'sleep' cuts mains and 'standby' keeps the
        # network/EPG sweep alive while silencing HDMI.
        ok, reason = await openwebif_standby(rcfg)
    else:
        raise HTTPException(400, "action must be 'wake', 'sleep', or 'standby'")
    return {"ok": ok, "receiver": receiver, "action": action,
            "power_method": rcfg.power_method, "reason": reason}
