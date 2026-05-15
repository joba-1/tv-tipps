"""User likes: thumb-up an EPG event to feed positive signal into recommendations."""
from __future__ import annotations
from fastapi import APIRouter, Cookie, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, UserLike, EpgEvent, Channel, RecommendationCache
from app.timezones import utcnow

router = APIRouter()


class LikeToggleRequest(BaseModel):
    epg_event_id: int


def _current_user(slug: str | None, db: Session) -> User | None:
    if not slug:
        return None
    return db.query(User).filter_by(slug=slug).first()


@router.get("/api/likes")
def list_likes(
    tv_tips_user: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    user = _current_user(tv_tips_user, db)
    if not user:
        return []
    rows = (
        db.query(UserLike)
        .filter(UserLike.user_id == user.id)
        .order_by(UserLike.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "epg_event_id": r.epg_event_id,
            "title": r.title,
            "channel_name": r.channel_name,
            "genre": r.genre,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/api/likes/toggle")
def toggle_like(
    req: LikeToggleRequest,
    tv_tips_user: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    user = _current_user(tv_tips_user, db)
    if not user:
        raise HTTPException(401, "No user cookie set")

    existing = (
        db.query(UserLike)
        .filter_by(user_id=user.id, epg_event_id=req.epg_event_id)
        .first()
    )
    if existing:
        db.delete(existing)
        db.query(RecommendationCache).filter_by(user_id=user.id).delete()
        db.commit()
        return {"liked": False, "epg_event_id": req.epg_event_id}

    event = db.get(EpgEvent, req.epg_event_id)
    if not event:
        raise HTTPException(404, "EPG event not found")
    channel = db.get(Channel, event.channel_id)
    db.add(UserLike(
        user_id=user.id,
        epg_event_id=req.epg_event_id,
        title=event.title,
        channel_name=channel.name if channel else None,
        genre=event.genre,
        created_at=utcnow(),
    ))
    db.query(RecommendationCache).filter_by(user_id=user.id).delete()
    db.commit()
    return {"liked": True, "epg_event_id": req.epg_event_id}
