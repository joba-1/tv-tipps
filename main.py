from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
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


app = FastAPI(title="tv-tipps", lifespan=lifespan)


@app.exception_handler(Exception)
async def _dump_unhandled(request: Request, exc: Exception):
    """Last-resort catch for unhandled handler exceptions: dump the request
    context to disk so we can investigate after the fact. HTTPException and
    RequestValidationError have their own handlers and are not caught here."""
    if isinstance(exc, (StarletteHTTPException, RequestValidationError)):
        raise exc
    from app.services.forensics import dump_failure
    try:
        body = (await request.body()).decode("utf-8", errors="replace")[:4096]
    except Exception:
        body = ""
    dump_failure("request", tag=f"{request.method}_{request.url.path[:32]}",
                 method=request.method, url=str(request.url),
                 client=request.client.host if request.client else None,
                 error=f"{type(exc).__name__}: {exc}",
                 body=body)
    log.error("request.unhandled", method=request.method,
              path=request.url.path, error=str(exc))
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

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

# iOS Safari probes the document root for /apple-touch-icon*.png when the user
# taps "Add to Home Screen", even though we also link it via <link rel> in the
# HTML. Without these root routes, Safari falls back to rendering the first
# letter of the page title (the dreaded generic "T" tile).
_apple_icon = static_dir / "apple-touch-icon.png"


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
@app.get("/apple-touch-icon-180x180.png", include_in_schema=False)
@app.get("/apple-touch-icon-180x180-precomposed.png", include_in_schema=False)
async def apple_touch_icon():
    if _apple_icon.exists():
        return FileResponse(_apple_icon, media_type="image/png")
    return HTMLResponse(status_code=404)


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    index = static_dir / "index.html"
    if not index.exists():
        return {"detail": "Frontend not built yet"}
    html = index.read_text()
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={_version}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={_version}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
