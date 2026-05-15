from fastapi import APIRouter, Depends, Cookie
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.epg import get_now_next
from app.services.channels import get_channels_for_user
from app.schemas import NowNextOut

router = APIRouter()


@router.get("/api/now-next", response_model=list[NowNextOut])
def now_next(
    tv_tips_user: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    from app.models import User
    user_id = None
    if tv_tips_user:
        user = db.query(User).filter_by(slug=tv_tips_user).first()
        if user:
            user_id = user.id

    channels = get_channels_for_user(user_id, db)
    channel_ids = [c.id for c in channels]
    return get_now_next(channel_ids, db)
