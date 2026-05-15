from fastapi import APIRouter, Depends, Query, Cookie, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User

router = APIRouter()

_VALID_CONTEXTS = ("now", "next", "prime", "today")


@router.get("/api/recommendations")
async def get_recommendations(
    context: str = Query(default="now", description="now | next | prime | today"),
    tv_tips_user: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    if context not in _VALID_CONTEXTS:
        raise HTTPException(400, f"context must be one of: {', '.join(_VALID_CONTEXTS)}")

    user = None
    if tv_tips_user:
        user = db.query(User).filter_by(slug=tv_tips_user).first()
    if not user:
        user = db.query(User).first()
    if not user:
        return {
            "context": context, "user_name": "", "taste_summary": "",
            "recommendations": [], "cached": False, "cold_start": True,
        }

    from app.services.recommendations import get_recommendations as _get
    return await _get(user.id, user.name, context, db)
