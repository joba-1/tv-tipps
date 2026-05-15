from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
from app.logging_setup import setup_logging, get_logger
from app.database import init_db, SessionLocal
from app.models import Receiver, User
from config import settings

setup_logging()
log = get_logger(__name__)


def seed_db() -> None:
    """Create receiver and user rows from config if they don't exist."""
    from datetime import datetime
    db = SessionLocal()
    try:
        for rcfg in settings.receivers:
            if not db.query(Receiver).filter_by(name=rcfg.name).first():
                db.add(Receiver(
                    name=rcfg.name,
                    ip=rcfg.ip,
                    default_user=rcfg.default_user,
                    power_state="unknown",
                ))
                log.info("db.seeded_receiver", name=rcfg.name)
        for ucfg in settings.users:
            if not db.query(User).filter_by(slug=ucfg.slug).first():
                db.add(User(slug=ucfg.slug, name=ucfg.name, created_at=datetime.utcnow()))
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
from app.routers import now_next, epg, admin  # noqa: E402
app.include_router(now_next.router)
app.include_router(epg.router)
app.include_router(admin.router)

# Static files
static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    index = static_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"detail": "Frontend not built yet"}
