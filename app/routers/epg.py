from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query, Cookie, HTTPException
from sqlalchemy import or_, func
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import EpgEvent, Channel
from app.services.epg import get_epg_range, _event_dict
from app.services.channels import get_channels_for_user
from app.timezones import utcnow, hours_range, tonight_range
from app.schemas import EpgRangeItem

router = APIRouter()


@router.get("/api/epg", response_model=list[EpgRangeItem])
def epg_range(
    hours: float | None = Query(default=None, description="Relative: next N hours"),
    start: str | None = Query(default=None, description="ISO UTC start"),
    end: str | None = Query(default=None, description="ISO UTC end"),
    context: str | None = Query(default=None, description="'tonight' or 'evening'"),
    tv_tipps_user: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    from app.models import User

    # Resolve time range
    if context in ("tonight", "evening"):
        start_dt, end_dt = tonight_range()
    elif hours is not None:
        if hours > 72:
            raise HTTPException(400, "Max 72 hours")
        start_dt, end_dt = hours_range(hours)
    elif start and end:
        try:
            start_dt = datetime.fromisoformat(start)
            end_dt = datetime.fromisoformat(end)
        except ValueError:
            raise HTTPException(400, "Invalid datetime format")
        if (end_dt - start_dt).total_seconds() > 72 * 3600:
            raise HTTPException(400, "Max 72 hours")
    else:
        start_dt, end_dt = hours_range(4)  # default: next 4h

    user_id = None
    if tv_tipps_user:
        user = db.query(User).filter_by(slug=tv_tipps_user).first()
        if user:
            user_id = user.id

    channels = get_channels_for_user(user_id, db)
    channel_ids = [c.id for c in channels]
    # Clamp start to now so already-ended events are never returned
    now = utcnow()
    if start_dt < now:
        start_dt = now
    items = get_epg_range(channel_ids, start_dt, end_dt, db, future_only=False)
    _decorate_with_match(items, user_id, db)
    return items


@router.get("/api/epg/search", response_model=list[EpgRangeItem])
def epg_search(
    q: str = Query(min_length=2, max_length=100, description="Free-text query"),
    days: int = Query(default=7, ge=1, le=14),
    tv_tipps_user: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    """Search future EPG events by title/short_desc/long_desc, case-insensitive."""
    from app.models import User

    user_id = None
    if tv_tipps_user:
        user = db.query(User).filter_by(slug=tv_tipps_user).first()
        if user:
            user_id = user.id

    channels = get_channels_for_user(user_id, db)
    channel_ids = [c.id for c in channels]
    if not channel_ids:
        return []

    now = utcnow()
    end = now + timedelta(days=days)
    pattern = f"%{q.lower()}%"

    rows = (
        db.query(EpgEvent, Channel)
        .join(Channel, EpgEvent.channel_id == Channel.id)
        .filter(
            EpgEvent.channel_id.in_(channel_ids),
            EpgEvent.end_time > now,
            EpgEvent.start_time < end,
            or_(
                func.lower(EpgEvent.title).like(pattern),
                func.lower(EpgEvent.short_desc).like(pattern),
                func.lower(EpgEvent.long_desc).like(pattern),
            ),
        )
        .order_by(EpgEvent.start_time.asc())
        .limit(50)
        .all()
    )

    result = []
    for ev, ch in rows:
        d = _event_dict(ev, now)
        d["channel_name"] = ch.name
        d["sref"] = ch.sref
        d["channel_id"] = ch.id
        result.append(d)
    _decorate_with_match(result, user_id, db)
    return result


def _decorate_with_match(items: list[dict], user_id: int | None, db: Session) -> None:
    """Populate match_score on each item if the user has a "good" stored score
    (>= GOOD_MATCH_THRESHOLD). Items without a good score keep match_score=None
    so the UI hides the chip."""
    if not user_id or not items:
        return
    from app.services.scoring import good_scores_for_events
    scores = good_scores_for_events(user_id, [i["id"] for i in items], db)
    for item in items:
        if item["id"] in scores:
            item["match_score"] = scores[item["id"]]
