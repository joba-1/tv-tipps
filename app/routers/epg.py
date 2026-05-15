from datetime import datetime
from fastapi import APIRouter, Depends, Query, Cookie, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.epg import get_epg_range
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
    tv_tips_user: str | None = Cookie(default=None),
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
    if tv_tips_user:
        user = db.query(User).filter_by(slug=tv_tips_user).first()
        if user:
            user_id = user.id

    channels = get_channels_for_user(user_id, db)
    channel_ids = [c.id for c in channels]
    return get_epg_range(channel_ids, start_dt, end_dt, db)
