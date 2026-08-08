"""Tests for app/services/scoring.py — the score-backed recommendation pipeline."""
from __future__ import annotations
import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from sqlalchemy.orm import Session
from app import models
from app.services.scoring import (
    _parse_scoring_response, _rule_score, _score_chunk, _upsert_scores,
    good_scores_for_events, set_explicit_score, clear_explicit_score,
    mark_user_llm_rows_stale, _stale_future_event_ids,
    _get_recent_history, _get_recent_reactions,
    get_recommendations_from_scores, GOOD_MATCH_THRESHOLD,
)
from app.timezones import utcnow
from tests.conftest import make_channel, make_event, make_user, make_session


# ── _parse_scoring_response ───────────────────────────────────────────────────

class TestParseScoringResponse:
    def test_valid_response(self):
        raw = {"scores": [{"score": 0.8, "reason": "passt"}, {"score": 0.2, "reason": "nö"}]}
        out = _parse_scoring_response(raw)
        assert out == [(0.8, "passt"), (0.2, "nö")]

    def test_non_dict_returns_empty(self):
        assert _parse_scoring_response(None) == []
        assert _parse_scoring_response("text") == []

    def test_scores_clamped_to_0_1(self):
        raw = {"scores": [{"score": 1.7, "reason": "a"}, {"score": -0.3, "reason": "b"}]}
        out = _parse_scoring_response(raw)
        assert out[0][0] == 1.0
        assert out[1][0] == 0.0

    def test_bad_score_defaults_to_neutral(self):
        raw = {"scores": [{"score": "hoch", "reason": "a"}]}
        assert _parse_scoring_response(raw)[0][0] == 0.5

    def test_non_dict_items_skipped(self):
        raw = {"scores": [{"score": 0.5, "reason": "ok"}, "junk", 3]}
        assert len(_parse_scoring_response(raw)) == 1

    def test_reason_truncated(self):
        raw = {"scores": [{"score": 0.5, "reason": "x" * 500}]}
        assert len(_parse_scoring_response(raw)[0][1]) == 240


# ── _rule_score fallback ──────────────────────────────────────────────────────

class TestRuleScore:
    def test_shopping_channel_near_zero(self, db: Session):
        ch = make_channel(db, name="QVC")
        ev = make_event(db, ch)
        assert _rule_score(ev, ch) <= 0.1

    def test_film_beats_news(self, db: Session):
        ch = make_channel(db)
        film = make_event(db, ch, genre="Spielfilm", title="Film")
        news = make_event(db, ch, genre="Nachrichten", title="News", offset_min=60)
        assert _rule_score(film, ch) > _rule_score(news, ch)

    def test_score_in_range(self, db: Session):
        ch = make_channel(db, name="ARD")
        ev = make_event(db, ch, genre="Spielfilm", duration_sec=5400)
        assert 0.0 <= _rule_score(ev, ch) <= 1.0


# ── _score_chunk: LLM interaction with fallback/halving ───────────────────────

def _chunk_of(db, n, name="Ch"):
    out = []
    for i in range(n):
        ch = make_channel(db, sref=f"1:0:1:{i}:9:9:0:0:0:0:", name=f"{name}{i}")
        out.append((make_event(db, ch, title=f"Show {i}"), ch))
    db.commit()
    return out


class TestScoreChunk:
    @pytest.mark.asyncio
    async def test_llm_down_falls_back_to_rule(self, db: Session):
        chunk = _chunk_of(db, 3)
        with patch("app.services.scoring.ask_json", new_callable=AsyncMock, return_value=None):
            triples = await _score_chunk("Alice", {}, [], [], [], chunk)
        assert len(triples) == 3
        assert all(t[3] == "rule" for t in triples)

    @pytest.mark.asyncio
    async def test_valid_response_maps_positionally(self, db: Session):
        chunk = _chunk_of(db, 2)
        raw = {"scores": [{"score": 0.9, "reason": "top"}, {"score": 0.1, "reason": "flop"}]}
        usage = {"prompt_overflow": False, "completion_truncated": False}
        with patch("app.services.scoring.ask_json", new_callable=AsyncMock, return_value=raw), \
             patch("app.services.scoring._ollama.last_usage", return_value=usage):
            triples = await _score_chunk("Alice", {}, [], [], [], chunk)
        assert [(t[0], t[1], t[3]) for t in triples] == [
            (chunk[0][0].id, 0.9, "llm"), (chunk[1][0].id, 0.1, "llm"),
        ]

    @pytest.mark.asyncio
    async def test_length_mismatch_small_chunk_falls_back_to_rule(self, db: Session):
        chunk = _chunk_of(db, 3)  # ≤ 12 → no halving
        raw = {"scores": [{"score": 0.9, "reason": "only one"}]}
        usage = {"prompt_overflow": False, "completion_truncated": False}
        with patch("app.services.scoring.ask_json", new_callable=AsyncMock, return_value=raw), \
             patch("app.services.scoring._ollama.last_usage", return_value=usage):
            triples = await _score_chunk("Alice", {}, [], [], [], chunk)
        assert len(triples) == 3
        assert all(t[3] == "rule" for t in triples)

    @pytest.mark.asyncio
    async def test_length_mismatch_large_chunk_halves(self, db: Session):
        chunk = _chunk_of(db, 14)  # > 12 → halve into 7 + 7
        usage = {"prompt_overflow": False, "completion_truncated": False}

        calls = []

        async def fake_ask(prompt, caller="", format_schema=None):
            # Answer with as many entries as the schema demands, except on the
            # very first (full-size) call where one entry is dropped.
            n = format_schema["properties"]["scores"]["minItems"]
            calls.append(n)
            miss = 1 if len(calls) == 1 else 0
            return {"scores": [{"score": 0.5, "reason": "r"}] * (n - miss)}

        with patch("app.services.scoring.ask_json", side_effect=fake_ask), \
             patch("app.services.scoring._ollama.last_usage", return_value=usage):
            triples = await _score_chunk("Alice", {}, [], [], [], chunk)
        assert calls == [14, 7, 7]
        assert len(triples) == 14
        assert all(t[3] == "llm" for t in triples)


# ── score persistence helpers ─────────────────────────────────────────────────

class TestScorePersistence:
    def test_upsert_then_good_scores(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        hi = make_event(db, ch, title="Hi")
        lo = make_event(db, ch, title="Lo", offset_min=90)
        db.commit()
        _upsert_scores(user.id, [
            (hi.id, 0.9, "gut", "llm"),
            (lo.id, 0.3, None, "llm"),
        ], db)
        scores = good_scores_for_events(user.id, [hi.id, lo.id], db)
        assert hi.id in scores and scores[hi.id] == pytest.approx(0.9)
        assert lo.id not in scores  # below GOOD_MATCH_THRESHOLD
        assert 0.3 < GOOD_MATCH_THRESHOLD

    def test_upsert_overwrites_and_unstales(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        ev = make_event(db, ch)
        db.commit()
        _upsert_scores(user.id, [(ev.id, 0.4, None, "rule")], db)
        mark_user_llm_rows_stale(user.id, except_event_id=None, db=db)
        assert _stale_future_event_ids(user.id, db) == [ev.id]
        _upsert_scores(user.id, [(ev.id, 0.8, "neu", "llm")], db)
        assert _stale_future_event_ids(user.id, db) == []
        row = db.get(models.UserEventScore, (user.id, ev.id))
        assert row.match_score == pytest.approx(0.8)
        assert row.source == "llm"

    def test_explicit_score_set_and_clear(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        ev = make_event(db, ch)
        db.commit()
        set_explicit_score(user.id, ev.id, liked=True, db=db)
        row = db.get(models.UserEventScore, (user.id, ev.id))
        assert row.match_score == 1.0 and row.source == "explicit_like"
        # Explicit rows survive a stale-mark
        mark_user_llm_rows_stale(user.id, except_event_id=None, db=db)
        db.expire_all()
        assert db.get(models.UserEventScore, (user.id, ev.id)).stale is False
        clear_explicit_score(user.id, ev.id, db)
        assert db.get(models.UserEventScore, (user.id, ev.id)) is None


# ── profile context helpers ───────────────────────────────────────────────────

class TestProfileContext:
    def test_reactions_split_by_sentiment(self, db: Session):
        user = make_user(db)
        now = utcnow()
        db.add(models.UserLike(user_id=user.id, title="Good", sentiment="like",
                               created_at=now))
        db.add(models.UserLike(user_id=user.id, title="Bad", sentiment="dislike",
                               created_at=now))
        db.commit()
        likes, dislikes = _get_recent_reactions(user.id, db)
        assert [l["title"] for l in likes] == ["Good"]
        assert [d["title"] for d in dislikes] == ["Bad"]

    def test_history_only_confirmed_recent(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        make_session(db, user, ch, confirmed=True, days_ago=1)
        make_session(db, user, ch, confirmed=False, days_ago=1)
        make_session(db, user, ch, confirmed=True, days_ago=40)  # outside 30d window
        db.commit()
        history = _get_recent_history(user.id, db)
        assert len(history) == 1


# ── get_recommendations_from_scores ───────────────────────────────────────────

def _scored_setup(db):
    """User + two channels with one currently-airing event each, pre-scored."""
    user = make_user(db)
    ch_a = make_channel(db, sref="1:0:1:A:1:1:0:0:0:0:", name="ChanA")
    ch_b = make_channel(db, sref="1:0:1:B:1:1:0:0:0:0:", name="ChanB")
    ev_a = make_event(db, ch_a, title="Alpha", offset_min=-10, duration_sec=5400)
    ev_b = make_event(db, ch_b, title="Beta", offset_min=-10, duration_sec=5400)
    db.commit()
    _upsert_scores(user.id, [(ev_a.id, 0.9, "top", "llm"),
                             (ev_b.id, 0.6, "meh", "llm")], db)
    return user, [ch_a, ch_b], ev_a, ev_b


class TestGetRecommendationsFromScores:
    @pytest.mark.asyncio
    async def test_now_context_orders_by_score(self, db: Session):
        user, channels, ev_a, ev_b = _scored_setup(db)
        with patch("app.services.scoring.get_channels_for_user", return_value=channels):
            result = await get_recommendations_from_scores(user.id, user.name, "now", db)
        titles = [r["title"] for r in result["recommendations"]]
        assert titles == ["Alpha", "Beta"]
        assert result["recommendations"][0]["match_score"] == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_next_context_excludes_running_events(self, db: Session):
        user, channels, ev_a, ev_b = _scored_setup(db)
        ch = channels[0]
        soon = make_event(db, ch, title="Soon", offset_min=30)
        db.commit()
        _upsert_scores(user.id, [(soon.id, 0.5, None, "llm")], db)
        with patch("app.services.scoring.get_channels_for_user", return_value=channels):
            result = await get_recommendations_from_scores(user.id, user.name, "next", db)
        titles = [r["title"] for r in result["recommendations"]]
        assert titles == ["Soon"]

    @pytest.mark.asyncio
    async def test_explicit_like_tops_list(self, db: Session):
        user, channels, ev_a, ev_b = _scored_setup(db)
        set_explicit_score(user.id, ev_b.id, liked=True, db=db)
        with patch("app.services.scoring.get_channels_for_user", return_value=channels):
            result = await get_recommendations_from_scores(user.id, user.name, "now", db)
        assert result["recommendations"][0]["title"] == "Beta"
        assert result["recommendations"][0]["match_score"] == 1.0

    @pytest.mark.asyncio
    async def test_parallel_feeds_deduped(self, db: Session):
        """Same title + start_time on two channels → only the higher score survives."""
        user = make_user(db)
        ch_a = make_channel(db, sref="1:0:1:A:1:1:0:0:0:0:", name="ZDF")
        ch_b = make_channel(db, sref="1:0:1:B:1:1:0:0:0:0:", name="ZDFneo")
        ev_a = make_event(db, ch_a, title="Der Film", offset_min=-10, duration_sec=5400)
        ev_b = make_event(db, ch_b, title="Der Film", offset_min=-10, duration_sec=5400)
        # Parallel feeds share the exact broadcast slot; align the timestamps
        # make_event derived from two separate utcnow() calls.
        ev_b.start_time = ev_a.start_time
        ev_b.end_time = ev_a.end_time
        db.commit()
        _upsert_scores(user.id, [(ev_a.id, 0.8, None, "llm"),
                                 (ev_b.id, 0.7, None, "llm")], db)
        with patch("app.services.scoring.get_channels_for_user", return_value=[ch_a, ch_b]):
            result = await get_recommendations_from_scores(user.id, user.name, "now", db)
        recs = result["recommendations"]
        assert len(recs) == 1
        assert recs[0]["channel_name"] == "ZDF"

    @pytest.mark.asyncio
    async def test_unscored_events_get_inline_rule_score(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        make_event(db, ch, title="Fresh", offset_min=-10, duration_sec=5400)
        db.commit()
        with patch("app.services.scoring.get_channels_for_user", return_value=[ch]), \
             patch("app.services.scoring.score_events_for_user", new_callable=AsyncMock):
            result = await get_recommendations_from_scores(user.id, user.name, "now", db)
        assert result["regenerating"] is True
        assert result["cold_start"] is True  # no stored scores yet
        assert [r["title"] for r in result["recommendations"]] == ["Fresh"]

    @pytest.mark.asyncio
    async def test_now_drops_nearly_ended(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        ending = make_event(db, ch, title="Ending", offset_min=-55, duration_sec=3600)
        db.commit()
        _upsert_scores(user.id, [(ending.id, 0.9, None, "llm")], db)
        with patch("app.services.scoring.get_channels_for_user", return_value=[ch]):
            result = await get_recommendations_from_scores(user.id, user.name, "now", db)
        assert result["recommendations"] == []

    @pytest.mark.asyncio
    async def test_now_bridges_slot_boundary(self, db: Session):
        """Everything airing is nearly over (the pre-:15 situation) → the list
        must still fill from the events starting in the next few minutes."""
        user = make_user(db)
        ch = make_channel(db)
        ending = make_event(db, ch, title="Ending", offset_min=-55, duration_sec=3600)
        starting = make_event(db, ch, title="Starting", offset_min=5, duration_sec=3600)
        too_far = make_event(db, ch, title="TooFar", offset_min=40, duration_sec=3600)
        db.commit()
        _upsert_scores(user.id, [(ending.id, 0.9, None, "llm"),
                                 (starting.id, 0.6, None, "llm"),
                                 (too_far.id, 0.95, None, "llm")], db)
        with patch("app.services.scoring.get_channels_for_user", return_value=[ch]):
            result = await get_recommendations_from_scores(user.id, user.name, "now", db)
        assert [r["title"] for r in result["recommendations"]] == ["Starting"]
