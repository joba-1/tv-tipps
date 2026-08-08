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


# ── _upsert_events: change detection + overlap unification ───────────────────

def _parsed(sref: str, *, title: str = "Show", start_offset_min: int = 30,
            duration_min: int = 60, genre: str | None = None,
            short_desc: str | None = None):
    from app.enigma.parser import ParsedEpgEvent
    start = utcnow() + timedelta(minutes=start_offset_min)
    return ParsedEpgEvent(
        sref=sref, channel_name="Chan", event_id=None, title=title,
        short_desc=short_desc, long_desc=None,
        start_time=start, end_time=start + timedelta(minutes=duration_min),
        duration_sec=duration_min * 60, genre=genre,
    )


class TestUpsertEvents:
    def test_inserts_new_events_without_changes(self, db: Session):
        from app.services.epg import _upsert_events
        ch = make_channel(db)
        count, changed = _upsert_events(ch, [_parsed(ch.sref)], db, utcnow())
        db.commit()
        assert count == 1
        assert changed == []
        assert db.query(models.EpgEvent).count() == 1

    def test_genre_enrichment_reports_change(self, db: Session):
        from app.services.epg import _upsert_events
        ch = make_channel(db)
        ev = _parsed(ch.sref, genre=None)
        _upsert_events(ch, [ev], db, utcnow())
        db.commit()
        existing_id = db.query(models.EpgEvent).one().id
        richer = _parsed(ch.sref, genre="Krimi")
        richer.start_time = ev.start_time
        richer.end_time = ev.end_time
        _, changed = _upsert_events(ch, [richer], db, utcnow())
        db.commit()
        assert changed == [existing_id]
        assert db.query(models.EpgEvent).one().genre == "Krimi"

    def test_null_genre_does_not_blank_known_value(self, db: Session):
        from app.services.epg import _upsert_events
        ch = make_channel(db)
        rich = _parsed(ch.sref, genre="Krimi")
        _upsert_events(ch, [rich], db, utcnow())
        db.commit()
        sparse = _parsed(ch.sref, genre=None)
        sparse.start_time = rich.start_time
        sparse.end_time = rich.end_time
        _, changed = _upsert_events(ch, [sparse], db, utcnow())
        db.commit()
        assert changed == []  # coalesce kept the genre → no LLM-relevant change
        assert db.query(models.EpgEvent).one().genre == "Krimi"

    def test_shifted_event_removes_overlapped_row(self, db: Session):
        from app.services.epg import _upsert_events
        ch = make_channel(db)
        old = _add_event(db, ch, start_offset_min=60, title="Old Slot")
        old_id = old.id
        db.commit()
        shifted = _parsed(ch.sref, title="Old Slot", start_offset_min=75)
        _upsert_events(ch, [shifted], db, utcnow())
        db.commit()
        db.expire_all()
        rows = db.query(models.EpgEvent).all()
        assert len(rows) == 1
        assert rows[0].start_time == shifted.start_time
        assert rows[0].id != old_id

    def test_overlapped_row_with_session_survives(self, db: Session):
        from app.services.epg import _upsert_events
        from tests.conftest import make_user, make_receiver
        ch = make_channel(db)
        user = make_user(db)
        rcv = make_receiver(db)
        old = _add_event(db, ch, start_offset_min=60, title="Pinned")
        db.add(models.ViewingSession(
            user_id=user.id, channel_id=ch.id, receiver_id=rcv.id,
            epg_event_id=old.id, started_at=utcnow(),
            confirmed=True, source="ir_remote",
        ))
        db.commit()
        shifted = _parsed(ch.sref, title="Pinned", start_offset_min=75)
        _upsert_events(ch, [shifted], db, utcnow())
        db.commit()
        assert db.query(models.EpgEvent).count() == 2  # both rows kept


class TestCleanupOverlappingEvents:
    def test_keeps_newest_cached_row(self, db: Session):
        from app.services.epg import cleanup_overlapping_events
        ch = make_channel(db)
        now = utcnow()
        old = _add_event(db, ch, start_offset_min=60, title="Old")
        new = _add_event(db, ch, start_offset_min=75, title="New")
        old.cached_at = now - timedelta(hours=5)
        untouched = _add_event(db, ch, start_offset_min=300, title="Later")
        db.commit()
        deleted = cleanup_overlapping_events(db)
        titles = sorted(t for (t,) in db.query(models.EpgEvent.title).all())
        assert deleted == 1
        assert titles == ["Later", "New"]

    def test_past_overlaps_untouched(self, db: Session):
        from app.services.epg import cleanup_overlapping_events
        ch = make_channel(db)
        _add_event(db, ch, start_offset_min=-120, title="PastA")
        _add_event(db, ch, start_offset_min=-90, title="PastB")
        db.commit()
        assert cleanup_overlapping_events(db) == 0
        assert db.query(models.EpgEvent).count() == 2


# ── refresh_now_next: stale-marking + scoring enqueue ─────────────────────────

class _FakeEpgClient:
    def __init__(self, now_events, next_events=None):
        self._now = now_events
        self._next = next_events or []

    async def get_epg_now(self, bref):
        return {"events": self._now}

    async def get_epg_next(self, bref):
        return {"events": self._next}


def _raw_event(sref: str, *, title: str = "Show", genre: str | None = None,
               start_offset_min: int = 5, duration_min: int = 60) -> dict:
    from datetime import timezone as _tzu
    begin = utcnow().replace(tzinfo=_tzu.utc) + timedelta(minutes=start_offset_min)
    return {
        "sref": sref, "sname": "Chan", "id": 7, "title": title,
        "shortdesc": None, "longdesc": None, "genre": genre,
        "begin_timestamp": int(begin.timestamp()),
        "duration_sec": duration_min * 60,
    }


class TestRefreshNowNext:
    @pytest.mark.asyncio
    async def test_change_marks_scores_stale_and_enqueues(self, db: Session, monkeypatch):
        from app.services import epg as epg_service
        from tests.conftest import make_user, make_receiver
        ch = make_channel(db)
        user = make_user(db)
        rcv = make_receiver(db)
        db.add(models.Bouquet(receiver_id=rcv.id, bref="bref1", name="Favs", position=0))
        db.commit()

        # Seed the event without genre plus a fresh score row for it.
        raw = _raw_event(ch.sref, genre=None)
        await epg_service.refresh_now_next(rcv, _FakeEpgClient([raw]), db)
        ev = db.query(models.EpgEvent).one()
        db.add(models.UserEventScore(
            user_id=user.id, epg_event_id=ev.id, match_score=0.5,
            source="llm", rated_at=utcnow(), stale=False,
        ))
        db.commit()

        enqueued: list[list[int]] = []
        monkeypatch.setattr(epg_service, "enqueue_scoring", enqueued.append)

        # Same event again, now with genre → LLM-relevant change.
        richer = dict(raw, genre="Krimi")
        await epg_service.refresh_now_next(rcv, _FakeEpgClient([richer]), db)

        db.expire_all()
        score = db.query(models.UserEventScore).one()
        assert score.stale is True
        assert enqueued and ev.id in enqueued[0]


# ── Transponder priming (nightly zap tour) ────────────────────────────────────

from app.services.epg import transponder_key, transponder_tour, prime_epg_cache  # noqa: E402


class TestTransponderKey:
    def test_extracts_tsid_onid_namespace(self):
        # type:flags:serviceType:SID:TSID:ONID:namespace:...
        assert transponder_key("1:0:19:2B98:3F2:1:C00000:0:0:0:") == ("3F2", "1", "C00000")

    def test_same_transponder_different_service(self):
        a = transponder_key("1:0:19:2B98:3F2:1:C00000:0:0:0:")
        b = transponder_key("1:0:19:2B99:3F2:1:C00000:0:0:0:")
        assert a == b

    def test_case_insensitive(self):
        assert (transponder_key("1:0:19:2b98:3f2:1:c00000:0:0:0:")
                == transponder_key("1:0:19:2B98:3F2:1:C00000:0:0:0:"))

    def test_malformed_sref(self):
        assert transponder_key("1:0:19") is None
        assert transponder_key("") is None


class TestTransponderTour:
    def test_one_channel_per_transponder(self, db: Session):
        from tests.conftest import make_channel
        a1 = make_channel(db, sref="1:0:19:1:AAA:1:C00000:0:0:0:", name="A one")
        a2 = make_channel(db, sref="1:0:19:2:AAA:1:C00000:0:0:0:", name="A two")
        b1 = make_channel(db, sref="1:0:19:3:BBB:1:C00000:0:0:0:", name="B one")
        db.commit()
        tour = transponder_tour([a1, a2, b1])
        assert len(tour) == 2
        assert {transponder_key(c.sref) for c in tour} == {("AAA", "1", "C00000"),
                                                           ("BBB", "1", "C00000")}

    def test_deterministic_route(self, db: Session):
        from tests.conftest import make_channel
        x = make_channel(db, sref="1:0:19:1:AAA:1:C00000:0:0:0:", name="Zeta")
        y = make_channel(db, sref="1:0:19:2:AAA:1:C00000:0:0:0:", name="Alpha")
        db.commit()
        # Same transponder → the alphabetically first channel represents it,
        # whichever order they arrive in.
        assert transponder_tour([x, y])[0].name == "Alpha"
        assert transponder_tour([y, x])[0].name == "Alpha"

    def test_malformed_srefs_skipped(self, db: Session):
        from tests.conftest import make_channel
        good = make_channel(db, sref="1:0:19:1:AAA:1:C00000:0:0:0:", name="Good")
        bad = make_channel(db, sref="nonsense", name="Bad")
        db.commit()
        assert [c.name for c in transponder_tour([good, bad])] == ["Good"]


class TestPrimeEpgCache:
    @pytest.mark.asyncio
    async def test_visits_each_transponder_once(self, db: Session, monkeypatch):
        from tests.conftest import make_channel, make_receiver
        zapped: list[str] = []

        class FakeClient:
            async def zap(self, sref):
                zapped.append(sref)
                return True

        rcv = make_receiver(db)
        chans = [
            make_channel(db, sref="1:0:19:1:AAA:1:C00000:0:0:0:", name="A one"),
            make_channel(db, sref="1:0:19:2:AAA:1:C00000:0:0:0:", name="A two"),
            make_channel(db, sref="1:0:19:3:BBB:1:C00000:0:0:0:", name="B one"),
        ]
        db.commit()
        out = await prime_epg_cache(rcv, FakeClient(), chans, dwell_sec=0)
        assert out == {"transponders": 2, "visited": 2}
        assert len(zapped) == 2

    @pytest.mark.asyncio
    async def test_failed_zap_does_not_abort_the_tour(self, db: Session):
        from tests.conftest import make_channel, make_receiver
        attempts: list[str] = []

        class FlakyClient:
            async def zap(self, sref):
                attempts.append(sref)
                return len(attempts) != 1  # first transponder refuses

        rcv = make_receiver(db)
        chans = [
            make_channel(db, sref="1:0:19:1:AAA:1:C00000:0:0:0:", name="A"),
            make_channel(db, sref="1:0:19:2:BBB:1:C00000:0:0:0:", name="B"),
        ]
        db.commit()
        out = await prime_epg_cache(rcv, FlakyClient(), chans, dwell_sec=0)
        assert len(attempts) == 2      # kept going
        assert out["visited"] == 1     # but only one landed
