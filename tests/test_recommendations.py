"""Tests for app/services/recommendations.py — rule-based ranking and cold-start logic."""
from __future__ import annotations
import json
import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from sqlalchemy.orm import Session
from app.services.recommendations import rule_based_rank, get_recommendations
from app import models
from app.timezones import utcnow
from tests.conftest import make_channel, make_event, make_user


# ── rule_based_rank ───────────────────────────────────────────────────────────

class TestRuleBasedRank:
    def test_returns_list(self, db: Session):
        ch = make_channel(db)
        ev = make_event(db, ch, duration_sec=3600)
        db.commit()
        result = rule_based_rank([ev], "now", db)
        assert isinstance(result, list)

    def test_shopping_channel_excluded(self, db: Session):
        ch = make_channel(db, sref="1:0:1:10:1:1:0:0:0:0:", name="QVC")
        ev = make_event(db, ch, duration_sec=3600)
        db.commit()
        result = rule_based_rank([ev], "now", db)
        assert result == []

    def test_teleshopping_excluded(self, db: Session):
        ch = make_channel(db, name="Teleshopping Plus")
        ev = make_event(db, ch, duration_sec=3600)
        db.commit()
        result = rule_based_rank([ev], "now", db)
        assert result == []

    def test_short_event_excluded_in_prime(self, db: Session):
        ch = make_channel(db)
        ev = make_event(db, ch, duration_sec=600)  # 10 min < 20 min threshold
        db.commit()
        result = rule_based_rank([ev], "prime", db)
        assert result == []

    def test_short_event_allowed_in_now(self, db: Session):
        ch = make_channel(db)
        ev = make_event(db, ch, duration_sec=600)
        db.commit()
        result = rule_based_rank([ev], "now", db)
        assert len(result) == 1

    def test_news_genre_lower_score(self, db: Session):
        ch_news = make_channel(db, sref="1:0:1:11:1:1:0:0:0:0:", name="NewsChannel")
        ch_film = make_channel(db, sref="1:0:1:12:1:1:0:0:0:0:", name="FilmChannel")
        ev_news = make_event(db, ch_news, genre="Nachrichten", duration_sec=3600, title="News Show")
        ev_film = make_event(db, ch_film, genre="Spielfilm", duration_sec=3600, title="Film Show")
        db.commit()
        result = rule_based_rank([ev_news, ev_film], "now", db)
        titles = [r["title"] for r in result]
        assert titles.index("Film Show") < titles.index("News Show")

    def test_film_genre_higher_score(self, db: Session):
        ch = make_channel(db, sref="1:0:1:20:1:1:0:0:0:0:", name="MovieCh")
        ev = make_event(db, ch, genre="Spielfilm", duration_sec=3600)
        db.commit()
        result = rule_based_rank([ev], "now", db)
        assert result[0]["match_score"] > 0.5

    def test_major_channel_higher_score(self, db: Session):
        ch_major = make_channel(db, sref="1:0:1:30:1:1:0:0:0:0:", name="ARD")
        ch_minor = make_channel(db, sref="1:0:1:31:1:1:0:0:0:0:", name="LocalTV")
        ev_major = make_event(db, ch_major, genre=None, duration_sec=3600, title="ARD Show")
        ev_minor = make_event(db, ch_minor, genre=None, duration_sec=3600, title="Local Show")
        db.commit()
        result = rule_based_rank([ev_major, ev_minor], "now", db)
        titles = [r["title"] for r in result]
        assert titles.index("ARD Show") < titles.index("Local Show")

    def test_result_has_required_fields(self, db: Session):
        ch = make_channel(db)
        ev = make_event(db, ch, duration_sec=3600)
        db.commit()
        result = rule_based_rank([ev], "now", db)
        assert len(result) == 1
        r = result[0]
        for field in ("id", "sref", "channel_name", "title", "start_time", "end_time",
                      "match_score", "reason"):
            assert field in r

    def test_capped_at_8_results(self, db: Session):
        channels = [make_channel(db, sref=f"1:0:1:{i}:1:1:0:0:0:0:", name=f"Ch{i}") for i in range(10)]
        events = [make_event(db, ch, duration_sec=3600, title=f"Show {i}") for i, ch in enumerate(channels)]
        db.commit()
        result = rule_based_rank(events, "now", db)
        assert len(result) <= 8

    def test_match_score_in_range(self, db: Session):
        ch = make_channel(db)
        ev = make_event(db, ch, duration_sec=3600)
        db.commit()
        result = rule_based_rank([ev], "now", db)
        for r in result:
            assert 0.0 <= r["match_score"] <= 1.0


# ── cold-start logic ──────────────────────────────────────────────────────────

class TestColdStartLogic:
    @pytest.mark.asyncio
    async def test_cold_start_skips_ai(self, db: Session):
        """With no sessions, no likes, no stated prefs → rule-based, ai not called."""
        user = make_user(db)
        ch = make_channel(db)
        now = utcnow()
        ev = models.EpgEvent(
            channel_id=ch.id, title="Test", start_time=now - timedelta(minutes=10),
            end_time=now + timedelta(hours=1), duration_sec=4200, cached_at=now,
        )
        db.add(ev)
        db.commit()

        with patch("app.services.recommendations.ask_json", new_callable=AsyncMock) as mock_ask:
            # Patch get_channels_for_user to return the channel
            with patch("app.services.recommendations.get_channels_for_user", return_value=[ch]):
                result = await get_recommendations(user.id, user.name, "now", db)

        mock_ask.assert_not_called()
        assert result["cold_start"] is True
        assert "recommendations" in result

    @pytest.mark.asyncio
    async def test_stated_prefs_bypass_cold_start(self, db: Session):
        """Stated preferences alone should bypass cold-start and call AI."""
        user = make_user(db)
        ch = make_channel(db)
        now = utcnow()
        ev = models.EpgEvent(
            channel_id=ch.id, title="Test", start_time=now - timedelta(minutes=10),
            end_time=now + timedelta(hours=1), duration_sec=4200, cached_at=now,
        )
        db.add(ev)
        # Store stated preferences in UserProfile
        profile_data = {
            "session_count": 0, "top_genres": [], "top_channels": [],
            "avg_duration_min": 0,
            "time_buckets": {"morning": 0, "afternoon": 0, "evening": 0, "late": 0},
            "stated_preferences": "Actionfilme",
        }
        db.add(models.UserProfile(
            user_id=user.id, computed_at=now, summary_json=json.dumps(profile_data)
        ))
        db.commit()

        ai_response = {
            "taste_summary": "Mag Action",
            "ranking": [1],
            "reasons": {"1": "Action film"},
        }
        with patch("app.services.recommendations.ask_json", new_callable=AsyncMock, return_value=ai_response):
            with patch("app.services.recommendations.get_channels_for_user", return_value=[ch]):
                result = await get_recommendations(user.id, user.name, "now", db)

        assert result["cold_start"] is False

    @pytest.mark.asyncio
    async def test_result_cached_on_second_call(self, db: Session):
        """Second call within TTL must return cached=True without calling AI again."""
        user = make_user(db)
        ch = make_channel(db)
        now = utcnow()
        ev = models.EpgEvent(
            channel_id=ch.id, title="Test", start_time=now - timedelta(minutes=10),
            end_time=now + timedelta(hours=1), duration_sec=4200, cached_at=now,
        )
        db.add(ev)
        db.commit()

        call_count = 0

        async def fake_ask(prompt):
            nonlocal call_count
            call_count += 1
            return {"taste_summary": "ok", "ranking": [1], "reasons": {"1": "great"}}

        with patch("app.services.recommendations.ask_json", side_effect=fake_ask):
            with patch("app.services.recommendations.get_channels_for_user", return_value=[ch]):
                # cold-start, so AI won't be called regardless
                r1 = await get_recommendations(user.id, user.name, "now", db)
                r2 = await get_recommendations(user.id, user.name, "now", db)

        assert r2["cached"] is True
        assert call_count == 0  # cold-start; if not cold-start the second call would still be cached


# ── _try_ollama index parsing ─────────────────────────────────────────────────

class TestTryOllama:
    @pytest.mark.asyncio
    async def test_valid_ranking_fills_metadata(self, db: Session):
        from app.services.recommendations import _try_ollama
        ch = make_channel(db)
        ev = make_event(db, ch, title="Great Movie", genre="Action", duration_sec=5400)
        db.commit()

        ai_response = {
            "taste_summary": "Loves action",
            "ranking": [1],
            "reasons": {"1": "Perfect match"},
        }
        with patch("app.services.recommendations.ask_json", new_callable=AsyncMock, return_value=ai_response):
            result = await _try_ollama("Alice", "now", {}, [], [], [], [ev], db)

        assert result is not None
        assert result["taste_summary"] == "Loves action"
        assert len(result["recommendations"]) == 1
        rec = result["recommendations"][0]
        assert rec["title"] == "Great Movie"
        assert rec["channel_name"] == ch.name
        assert rec["reason"] == "Perfect match"

    @pytest.mark.asyncio
    async def test_bad_llm_response_returns_none(self, db: Session):
        from app.services.recommendations import _try_ollama
        ch = make_channel(db)
        ev = make_event(db, ch)
        db.commit()

        with patch("app.services.recommendations.ask_json", new_callable=AsyncMock, return_value=None):
            result = await _try_ollama("Alice", "now", {}, [], [], [], [ev], db)

        assert result is None

    @pytest.mark.asyncio
    async def test_out_of_range_index_ignored(self, db: Session):
        from app.services.recommendations import _try_ollama
        ch = make_channel(db)
        ev = make_event(db, ch, title="Real Show")
        db.commit()

        ai_response = {
            "taste_summary": "ok",
            "ranking": [99, 1],  # 99 is out of range, 1 is valid
            "reasons": {},
        }
        with patch("app.services.recommendations.ask_json", new_callable=AsyncMock, return_value=ai_response):
            result = await _try_ollama("Alice", "now", {}, [], [], [], [ev], db)

        assert result is not None
        assert len(result["recommendations"]) == 1
        assert result["recommendations"][0]["title"] == "Real Show"
