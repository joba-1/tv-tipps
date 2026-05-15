"""Shared pytest fixtures: in-memory SQLite DB, model factories."""
from __future__ import annotations
import os
import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker, Session
from app.database import Base
from app import models  # noqa: F401 — registers all mapped classes

# Ensure i18n file reads work from the project root regardless of where pytest is invoked.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
os.chdir(_PROJECT_ROOT)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @sa_event.listens_for(engine, "connect")
    def _pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


# ── model factories ───────────────────────────────────────────────────────────

def make_channel(db: Session, *, sref: str = "1:0:1:1:1:1:0:0:0:0:", name: str = "TestChannel") -> models.Channel:
    ch = models.Channel(sref=sref, name=name, last_seen=_now())
    db.add(ch)
    db.flush()
    return ch


def make_event(
    db: Session,
    channel: models.Channel,
    *,
    title: str = "Test Show",
    genre: str | None = "Drama",
    duration_sec: int = 3600,
    offset_min: int = -30,  # minutes from now; negative = already started
) -> models.EpgEvent:
    now = _now()
    start = now + timedelta(minutes=offset_min)
    end = start + timedelta(seconds=duration_sec)
    ev = models.EpgEvent(
        channel_id=channel.id,
        title=title,
        start_time=start,
        end_time=end,
        duration_sec=duration_sec,
        genre=genre,
        cached_at=now,
    )
    db.add(ev)
    db.flush()
    return ev


def make_receiver(db: Session, *, name: str = "box1", ip: str = "192.168.1.1") -> models.Receiver:
    r = models.Receiver(name=name, ip=ip, power_state="on", last_seen=_now())
    db.add(r)
    db.flush()
    return r


def make_user(db: Session, *, slug: str = "alice", name: str = "Alice") -> models.User:
    u = models.User(slug=slug, name=name, created_at=_now())
    db.add(u)
    db.flush()
    return u


def make_session(
    db: Session,
    user: models.User,
    channel: models.Channel,
    *,
    confirmed: bool = True,
    genre: str | None = "Drama",
    duration_sec: int = 3600,
    epg_event: models.EpgEvent | None = None,
    days_ago: int = 0,
) -> models.ViewingSession:
    started = _now() - timedelta(days=days_ago, hours=1)
    ev_id = None
    if epg_event:
        ev_id = epg_event.id
    elif genre:
        ev = make_event(db, channel, genre=genre, duration_sec=duration_sec)
        ev_id = ev.id
    rcv = db.query(models.Receiver).first() or make_receiver(db)
    vs = models.ViewingSession(
        user_id=user.id,
        channel_id=channel.id,
        receiver_id=rcv.id,
        epg_event_id=ev_id,
        started_at=started,
        duration_sec=duration_sec,
        confirmed=confirmed,
        source="ir_remote",
    )
    db.add(vs)
    db.flush()
    return vs


def _now() -> datetime:
    from app.timezones import utcnow
    return utcnow()
