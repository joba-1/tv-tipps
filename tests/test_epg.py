"""Tests for app/services/epg.py — range queries, now/next, cleanup."""
from __future__ import annotations
import pytest
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.services.epg import get_epg_range, get_now_next, cleanup_old_events
from app import models
from app.timezones import utcnow
from tests.conftest import make_channel, make_event


def _add_event(
    db: Session,
    channel: models.Channel,
    *,
    start_offset_min: int,   # minutes from now
    duration_min: int = 60,
    title: str = "Show",
) -> models.EpgEvent:
    now = utcnow()
    start = now + timedelta(minutes=start_offset_min)
    end = start + timedelta(minutes=duration_min)
    ev = models.EpgEvent(
        channel_id=channel.id,
        title=title,
        start_time=start,
        end_time=end,
        duration_sec=duration_min * 60,
        cached_at=now,
    )
    db.add(ev)
    db.flush()
    return ev


# ── get_epg_range ─────────────────────────────────────────────────────────────

class TestGetEpgRange:
    def test_returns_event_in_window(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=30, title="Future Show")
        db.commit()
        now = utcnow()
        result = get_epg_range([ch.id], now, now + timedelta(hours=2), db)
        titles = [r["title"] for r in result]
        assert "Future Show" in titles

    def test_event_outside_window_excluded(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=180, title="Far Future")
        db.commit()
        now = utcnow()
        result = get_epg_range([ch.id], now, now + timedelta(hours=2), db)
        titles = [r["title"] for r in result]
        assert "Far Future" not in titles

    def test_future_only_excludes_current(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=-30, title="Currently Airing")
        _add_event(db, ch, start_offset_min=30, title="Starting Soon")
        db.commit()
        now = utcnow()
        result = get_epg_range([ch.id], now, now + timedelta(hours=3), db, future_only=True)
        titles = [r["title"] for r in result]
        assert "Currently Airing" not in titles
        assert "Starting Soon" in titles

    def test_overlap_mode_includes_current(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=-30, title="Currently Airing")
        db.commit()
        now = utcnow()
        result = get_epg_range([ch.id], now, now + timedelta(hours=2), db, future_only=False)
        titles = [r["title"] for r in result]
        assert "Currently Airing" in titles

    def test_ordered_by_start_time(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=90, title="Second")
        _add_event(db, ch, start_offset_min=30, title="First")
        db.commit()
        now = utcnow()
        result = get_epg_range([ch.id], now, now + timedelta(hours=3), db)
        titles = [r["title"] for r in result]
        assert titles.index("First") < titles.index("Second")

    def test_empty_channel_ids_returns_all(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=30, title="Any Channel Show")
        db.commit()
        now = utcnow()
        result = get_epg_range([], now, now + timedelta(hours=2), db)
        assert any(r["title"] == "Any Channel Show" for r in result)

    def test_result_has_channel_info(self, db: Session):
        ch = make_channel(db, name="TestCh")
        _add_event(db, ch, start_offset_min=10)
        db.commit()
        now = utcnow()
        result = get_epg_range([ch.id], now, now + timedelta(hours=2), db)
        assert result[0]["channel_name"] == "TestCh"
        assert result[0]["sref"] == ch.sref


# ── get_now_next ──────────────────────────────────────────────────────────────

class TestGetNowNext:
    def test_returns_entry_per_channel(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=-30, title="Now")
        db.commit()
        result = get_now_next([ch.id], db)
        assert len(result) == 1
        assert result[0]["channel_id"] == ch.id

    def test_now_event_detected(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=-30, title="On Now")
        db.commit()
        result = get_now_next([ch.id], db)
        assert result[0]["now"] is not None
        assert result[0]["now"]["title"] == "On Now"

    def test_next_event_detected(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=-30, title="Now Show", duration_min=60)
        _add_event(db, ch, start_offset_min=40, title="Next Show")
        db.commit()
        result = get_now_next([ch.id], db)
        assert result[0]["next"] is not None
        assert result[0]["next"]["title"] == "Next Show"

    def test_no_current_programme(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=60, title="Future Only")
        db.commit()
        result = get_now_next([ch.id], db)
        assert result[0]["now"] is None

    def test_progress_pct_in_range(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=-30, duration_min=60, title="Midway")
        db.commit()
        result = get_now_next([ch.id], db)
        pct = result[0]["now"]["progress_pct"]
        assert 0 < pct < 100


# ── cleanup_old_events ────────────────────────────────────────────────────────

class TestCleanupOldEvents:
    def test_deletes_old_events(self, db: Session):
        ch = make_channel(db)
        now = utcnow()
        old = models.EpgEvent(
            channel_id=ch.id, title="Old Show",
            start_time=now - timedelta(days=35),
            end_time=now - timedelta(days=35, hours=-1),
            duration_sec=3600, cached_at=now,
        )
        db.add(old)
        db.commit()
        deleted = cleanup_old_events(30, db)
        assert deleted == 1

    def test_keeps_recent_events(self, db: Session):
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=-60, title="Recent")
        db.commit()
        deleted = cleanup_old_events(30, db)
        assert deleted == 0

    def test_preserves_events_referenced_by_session(self, db: Session):
        from tests.conftest import make_user, make_receiver
        user = make_user(db)
        rcv = make_receiver(db)
        ch = make_channel(db)
        now = utcnow()
        ev = models.EpgEvent(
            channel_id=ch.id, title="Watched Long Ago",
            start_time=now - timedelta(days=40),
            end_time=now - timedelta(days=40, hours=-1),
            duration_sec=3600, cached_at=now,
        )
        db.add(ev)
        db.flush()
        vs = models.ViewingSession(
            user_id=user.id, channel_id=ch.id, receiver_id=rcv.id,
            epg_event_id=ev.id, started_at=now - timedelta(days=40),
            duration_sec=3600, confirmed=True, source="ir_remote",
        )
        db.add(vs)
        db.commit()
        deleted = cleanup_old_events(30, db)
        assert deleted == 0  # event is referenced, must not be deleted
