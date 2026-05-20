from fastapi import APIRouter, Depends, Cookie
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.epg import get_now_next
from app.services.channels import get_channels_for_user
from app.schemas import NowNextOut

router = APIRouter()


@router.get("/api/now-next", response_model=list[NowNextOut])
def now_next(
    tv_tipps_user: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    from app.models import User
    user_id = None
    if tv_tipps_user:
        user = db.query(User).filter_by(slug=tv_tipps_user).first()
        if user:
            user_id = user.id

    channels = get_channels_for_user(user_id, db)
    channel_ids = [c.id for c in channels]
    rows = get_now_next(channel_ids, db)

    # Decorate now/next event rows with the stored match_score, but only
    # surface "good" matches — the UI hides the badge otherwise.
    from app.services.scoring import good_scores_for_events
    event_ids = []
    for r in rows:
        if r.get("now"):  event_ids.append(r["now"]["id"])
        if r.get("next"): event_ids.append(r["next"]["id"])
    scores = good_scores_for_events(user_id, event_ids, db)
    for r in rows:
        for slot in ("now", "next"):
            ev = r.get(slot)
            if ev and ev["id"] in scores:
                ev["match_score"] = scores[ev["id"]]
    return rows
