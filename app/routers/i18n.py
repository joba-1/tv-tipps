from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.services import i18n as i18n_svc

router = APIRouter()


@router.get("/api/i18n/{lang}")
async def get_i18n(
    lang: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Return UI string dict for the requested language.

    status="ready"   → full translation available, use immediately.
    status="pending" → AI batch triggered; strings fall back to German for missing keys.
                       Frontend should poll every 15s until ready.
    """
    code = i18n_svc.normalize(lang)
    if not code:
        raise HTTPException(400, "Invalid language code (expected 2-3 alpha chars)")

    strings, missing = i18n_svc.get_translations(code, db)
    status = "ready"
    if missing:
        status = "pending"
        if not i18n_svc.is_translating(code):
            background_tasks.add_task(i18n_svc.translate_missing, code, missing)

    return {
        "lang": code,
        "status": status,
        "missing_count": len(missing),
        "strings": strings,
    }
