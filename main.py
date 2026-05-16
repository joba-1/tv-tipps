from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pathlib import Path
from app.logging_setup import setup_logging, get_logger
from app.database import init_db, SessionLocal
from app.models import Receiver, User
from config import settings

setup_logging()
log = get_logger(__name__)


def seed_db() -> None:
    """Bootstrap receivers/users from env vars — only when DB has none yet."""
    from datetime import datetime
    from app.timezones import utcnow
    db = SessionLocal()
    try:
        if settings.receivers_raw and db.query(Receiver).count() == 0:
            for rcfg in settings.receivers:
                db.add(Receiver(
                    name=rcfg.name, ip=rcfg.ip, default_user=rcfg.default_user,
                    location=rcfg.location, priority=rcfg.priority,
                    power_method=rcfg.power_method, wol_mac=rcfg.wol_mac,
                    has_genre=rcfg.has_genre, power_state="unknown",
                ))
                log.info("db.seeded_receiver", name=rcfg.name)
        if settings.users_raw and db.query(User).count() == 0:
            for ucfg in settings.users:
                db.add(User(slug=ucfg.slug, name=ucfg.name, created_at=utcnow()))
                log.info("db.seeded_user", slug=ucfg.slug)
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup.begin")
    init_db()
    seed_db()

    # Initial data fetch on startup
    from app.services.poller import start_scheduler, run_refresh
    await run_refresh("all")

    start_scheduler()
    log.info("startup.done")
    yield

    from app.services.poller import scheduler
    scheduler.shutdown(wait=False)
    log.info("shutdown.done")


app = FastAPI(title="tv-tips", lifespan=lifespan)

# Routers
from app.routers import now_next, epg, admin, recommendations, remote, i18n, likes  # noqa: E402
app.include_router(now_next.router)
app.include_router(epg.router)
app.include_router(admin.router)
app.include_router(recommendations.router)
app.include_router(remote.router)
app.include_router(i18n.router)
app.include_router(likes.router)

# Static files
static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

_version = (Path("VERSION").read_text().strip() if Path("VERSION").exists() else "0")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    index = static_dir / "index.html"
    if not index.exists():
        return {"detail": "Frontend not built yet"}
    html = index.read_text()
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={_version}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={_version}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
