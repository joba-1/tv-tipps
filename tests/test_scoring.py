"""Tests for app/services/scoring.py — the score-backed recommendation pipeline."""
from __future__ import annotations
import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from sqlalchemy.orm import Session
from app import models
from app.services.scoring import (
    _candidate_desc, _split_credits, _credits_line, _window_event_ids,
    _build_scoring_prompt, _CAND_DESC_MAX_CHARS, _CREDITS_LINE_MAX_CHARS,
    _parse_scoring_response, _align_to_chunk, _rule_score, _score_chunk,
    _upsert_scores,
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
        raw = {"scores": [{"index": 1, "score": 0.8, "reason": "passt"},
                          {"index": 2, "score": 0.2, "reason": "nö"}]}
        out = _parse_scoring_response(raw)
        assert out == [(1, 0.8, "passt"), (2, 0.2, "nö")]

    def test_non_dict_returns_empty(self):
        assert _parse_scoring_response(None) == []
        assert _parse_scoring_response("text") == []

    def test_scores_clamped_to_0_1(self):
        raw = {"scores": [{"index": 1, "score": 1.7, "reason": "a"},
                          {"index": 2, "score": -0.3, "reason": "b"}]}
        out = _parse_scoring_response(raw)
        assert out[0][1] == 1.0
        assert out[1][1] == 0.0

    def test_bad_score_defaults_to_neutral(self):
        raw = {"scores": [{"index": 1, "score": "hoch", "reason": "a"}]}
        assert _parse_scoring_response(raw)[0][1] == 0.5

    def test_non_dict_items_skipped(self):
        raw = {"scores": [{"index": 1, "score": 0.5, "reason": "ok"}, "junk", 3]}
        assert len(_parse_scoring_response(raw)) == 1

    def test_reason_truncated(self):
        raw = {"scores": [{"index": 1, "score": 0.5, "reason": "x" * 500}]}
        assert len(_parse_scoring_response(raw)[0][2]) == 240

    def test_missing_index_is_none(self):
        raw = {"scores": [{"score": 0.8, "reason": "a"}]}
        assert _parse_scoring_response(raw) == [(None, 0.8, "a")]

    def test_junk_index_is_none(self):
        raw = {"scores": [{"index": "vorne", "score": 0.8, "reason": "a"},
                          {"index": None, "score": 0.2, "reason": "b"},
                          {"index": 0, "score": 0.3, "reason": "c"}]}
        assert [t[0] for t in _parse_scoring_response(raw)] == [None, None, None]

    def test_numeric_string_index_is_kept(self):
        raw = {"scores": [{"index": "2", "score": 0.8, "reason": "a"}]}
        assert _parse_scoring_response(raw)[0][0] == 2


# ── _align_to_chunk ───────────────────────────────────────────────────────────

class TestAlignToChunk:
    def test_complete_index_set_is_sorted(self):
        parsed = [(3, 0.3, "c"), (1, 0.1, "a"), (2, 0.2, "b")]
        assert _align_to_chunk(parsed, 3) == [(1, 0.1, "a"), (2, 0.2, "b"), (3, 0.3, "c")]

    def test_correct_order_stays(self):
        parsed = [(1, 0.1, "a"), (2, 0.2, "b")]
        assert _align_to_chunk(parsed, 2) == parsed

    def test_no_indices_at_all_is_positional(self):
        # A model that ignores the field keeps the old positional behaviour.
        parsed = [(None, 0.1, "a"), (None, 0.2, "b")]
        assert _align_to_chunk(parsed, 2) == parsed

    def test_constant_index_is_treated_as_not_echoed(self):
        # A model filling the field with a constant has not attributed anything;
        # falling back to rule scores over that would be worse than positional.
        parsed = [(1, 0.1, "a"), (1, 0.2, "b")]
        assert _align_to_chunk(parsed, 2) == parsed

    def test_one_candidate_with_index_one(self):
        parsed = [(1, 0.4, "a")]
        assert _align_to_chunk(parsed, 1) == parsed

    def test_drifted_index_is_unattributable(self):
        # Candidate 2 answered twice, candidate 3 never → positional would be wrong.
        parsed = [(1, 0.1, "a"), (2, 0.2, "b"), (2, 0.3, "c")]
        assert _align_to_chunk(parsed, 3) is None

    def test_index_out_of_range_is_unattributable(self):
        parsed = [(1, 0.1, "a"), (3, 0.2, "b")]
        assert _align_to_chunk(parsed, 2) is None

    def test_partially_missing_indices_are_unattributable(self):
        parsed = [(1, 0.1, "a"), (None, 0.2, "b")]
        assert _align_to_chunk(parsed, 2) is None


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
    @pytest.mark.asyncio
    async def test_single_candidate_chunk_is_not_logged_as_constant(self, db: Session):
        # index=1 in a one-entry list is the complete set 1..1, not a filler.
        chunk = _chunk_of(db, 1)
        raw = {"scores": [{"index": 1, "score": 0.7, "reason": "ok"}]}
        usage = {"prompt_overflow": False, "completion_truncated": False}
        with patch("app.services.scoring.ask_json", new_callable=AsyncMock, return_value=raw), \
             patch("app.services.scoring._ollama.last_usage", return_value=usage), \
             patch("app.services.scoring.log") as log_mock:
            triples = await _score_chunk("Alice", {}, [], [], [], chunk)
        assert triples == [(chunk[0][0].id, 0.7, "ok", "llm")]
        assert not [c for c in log_mock.info.call_args_list
                    if c.args and "index_constant" in str(c.args[0])]

    @pytest.mark.asyncio
    async def test_valid_response_maps_positionally(self, db: Session):
        # No index in the response → the old positional matching still applies.
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
    async def test_shuffled_response_is_realigned_by_index(self, db: Session):
        # The model answered in another order; the index says what is what, so
        # every score still lands on its own event.
        chunk = _chunk_of(db, 3)
        raw = {"scores": [{"index": 3, "score": 0.3, "reason": "c"},
                          {"index": 1, "score": 0.9, "reason": "a"},
                          {"index": 2, "score": 0.5, "reason": "b"}]}
        usage = {"prompt_overflow": False, "completion_truncated": False}
        with patch("app.services.scoring.ask_json", new_callable=AsyncMock, return_value=raw), \
             patch("app.services.scoring._ollama.last_usage", return_value=usage):
            triples = await _score_chunk("Alice", {}, [], [], [], chunk)
        assert [(t[0], t[1], t[2]) for t in triples] == [
            (chunk[0][0].id, 0.9, "a"), (chunk[1][0].id, 0.5, "b"),
            (chunk[2][0].id, 0.3, "c"),
        ]

    @pytest.mark.asyncio
    async def test_duplicate_index_small_chunk_falls_back_to_rule(self, db: Session):
        # Right count, wrong attribution: positional matching would write both
        # scores onto the wrong events, so the batch is not used.
        chunk = _chunk_of(db, 3)
        raw = {"scores": [{"index": 1, "score": 0.9, "reason": "a"},
                          {"index": 1, "score": 0.1, "reason": "b"},
                          {"index": 2, "score": 0.5, "reason": "c"}]}
        usage = {"prompt_overflow": False, "completion_truncated": False}
        with patch("app.services.scoring.ask_json", new_callable=AsyncMock, return_value=raw), \
             patch("app.services.scoring._ollama.last_usage", return_value=usage):
            triples = await _score_chunk("Alice", {}, [], [], [], chunk)
        assert len(triples) == 3
        assert all(t[3] == "rule" for t in triples)

    @pytest.mark.asyncio
    async def test_drifted_index_large_chunk_halves(self, db: Session):
        chunk = _chunk_of(db, 14)
        usage = {"prompt_overflow": False, "completion_truncated": False}
        calls = []

        async def fake_ask(prompt, caller="", format_schema=None):
            n = format_schema["properties"]["scores"]["minItems"]
            calls.append(n)
            # Full-size call: the count is right but the model drifted — it
            # answered candidate 13 twice and never candidate 14. Positional
            # matching would silently shift, so the batch must be retried halved.
            idx = [i + 1 for i in range(n - 1)] + [n - 1] if len(calls) == 1 else None
            if idx is None:
                return {"scores": [{"index": i + 1, "score": 0.5, "reason": "r"}
                                   for i in range(n)]}
            return {"scores": [{"index": i, "score": 0.5, "reason": "r"} for i in idx]}

        with patch("app.services.scoring.ask_json", side_effect=fake_ask), \
             patch("app.services.scoring._ollama.last_usage", return_value=usage):
            triples = await _score_chunk("Alice", {}, [], [], [], chunk)
        assert calls == [14, 7, 7]
        assert len(triples) == 14
        assert all(t[3] == "llm" for t in triples)

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


# ── candidate EPG text ────────────────────────────────────────────────────────

class TestCandidateDesc:
    def test_joins_both_fields(self, db: Session):
        """short_desc is the structured line, long_desc the synopsis — the model
        needs both (they differ in 94 % of cases)."""
        ch = make_channel(db)
        ev = make_event(db, ch, short_desc="Scripted Reality, D 2018",
                        long_desc="Zwei Maler werden beschuldigt.")
        assert _candidate_desc(ev) == "Scripted Reality, D 2018 Zwei Maler werden beschuldigt."

    def test_only_one_field_present(self, db: Session):
        ch = make_channel(db)
        assert _candidate_desc(make_event(db, ch, long_desc="Nur lang")) == "Nur lang"
        assert _candidate_desc(make_event(db, ch, short_desc="Nur kurz")) == "Nur kurz"
        assert _candidate_desc(make_event(db, ch)) == ""

    def test_whitespace_collapsed(self, db: Session):
        ch = make_channel(db)
        ev = make_event(db, ch, short_desc="  Titel\n\nmit Umbruch  ", long_desc="Inhalt\t hier")
        assert _candidate_desc(ev) == "Titel mit Umbruch Inhalt hier"

    def test_capped(self, db: Session):
        ch = make_channel(db)
        ev = make_event(db, ch, short_desc="x" * 400, long_desc="y" * 400)
        out = _candidate_desc(ev)
        assert len(out) == _CAND_DESC_MAX_CHARS
        assert out.startswith("x" * 100)  # the structured line survives the cap
        assert "y" in out

    def test_credits_survive_the_synopsis_cap(self, db: Session):
        """Regression guard: a long synopsis used to push the credits block —
        director and cast — out of the prompt entirely (5.5 % of all blocks;
        the rest lost a median of 182 chars off the end)."""
        ch = make_channel(db)
        ev = make_event(db, ch, short_desc="Spielfilm, USA 1994",
                        long_desc="x" * 900 + " Regie: Quentin Tarantino "
                                  "Darsteller: John Travolta - Vincent Vega")
        out = _candidate_desc(ev)
        assert "Regie: Quentin Tarantino" in out
        assert "Darsteller: John Travolta - Vincent Vega" in out
        # synopsis cap + credits block, not one shared budget
        assert len(out) > _CAND_DESC_MAX_CHARS
        assert out.startswith("Spielfilm, USA 1994 xxx")

    def test_credits_inside_the_cap_are_not_duplicated(self, db: Session):
        ch = make_channel(db)
        ev = make_event(db, ch, short_desc="Kurz", long_desc="Synopsis. Regie: Jemand")
        assert _candidate_desc(ev) == "Kurz Synopsis. Regie: Jemand"

    def test_credits_block_capped_on_its_own(self, db: Session):
        ch = make_channel(db)
        ev = make_event(db, ch, long_desc="Synopsis." + " Darsteller: " + "z" * 900)
        out = _candidate_desc(ev)
        assert out.count("z") == _CAND_DESC_MAX_CHARS - len("Darsteller: ")


class TestSplitCredits:
    def test_splits_synopsis_and_block(self):
        syn, cred = _split_credits(
            "Zwei Maler streiten. Regie: Brad Bird Darsteller: A - B, C - D")
        assert syn == "Zwei Maler streiten."
        assert cred == "Regie: Brad Bird Darsteller: A - B, C - D"

    def test_without_block(self):
        assert _split_credits("Nur ein Text.") == ("Nur ein Text.", "")

    def test_does_not_fire_on_a_word_mention(self):
        """"Regie" ohne Doppelpunkt ist Prosa, kein Credits-Block."""
        syn, cred = _split_credits("Ein Film über die Regie in Hollywood.")
        assert syn == "Ein Film über die Regie in Hollywood." and cred == ""


class TestCreditsLine:
    def test_director_and_cast_in_priority_order(self):
        line = _credits_line(
            "Inhalt. Kamera: Jemand Musik: Anderer Regie: Brad Bird "
            "Darsteller: Remy - Patton Oswalt, Skinner - Ian Holm")
        assert line == "Regie: Brad Bird Darsteller: Remy - Patton Oswalt, Skinner - Ian Holm"

    def test_crew_only_yields_nothing(self):
        """A block that names only the crew says nothing about taste."""
        assert _credits_line("Inhalt. Kamera: Jemand Musik: Anderer") == ""

    def test_capped(self):
        line = _credits_line("Inhalt. Regie: " + "y" * 500)
        assert len(line) == _CREDITS_LINE_MAX_CHARS


class TestBuildScoringPrompt:
    def test_carries_both_epg_fields(self, db: Session):
        ch = make_channel(db)
        ev = make_event(db, ch, title="Kraven",
                        short_desc="Marvel-Actioner, USA 2024",
                        long_desc="Sergei Kravinoff findet in der Natur seinen Frieden.")
        prompt = _build_scoring_prompt("Alice", {}, [], [], [], [(ev, ch)])
        assert "Marvel-Actioner, USA 2024 Sergei Kravinoff" in prompt


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

    def test_reactions_are_not_capped_at_52(self, db: Session):
        """Regression guard: the cap used to be 52, which silently dropped the
        24 oldest likes of the heaviest account — i.e. the ones that defined the
        taste longest."""
        user = make_user(db)
        now = utcnow()
        for i in range(60):
            db.add(models.UserLike(user_id=user.id, title=f"Show {i}",
                                   sentiment="like", created_at=now))
        db.commit()
        likes, dislikes = _get_recent_reactions(user.id, db)
        assert len(likes) == 60
        assert dislikes == []

    def test_reactions_carry_the_credits_of_the_rated_event(self, db: Session):
        """Director and cast are a strong taste signal — the model can only use
        them if the liked film names them too."""
        user = make_user(db)
        ch = make_channel(db)
        ev = make_event(db, ch, title="Ratatouille",
                        long_desc="Ein Film. Regie: Brad Bird Kamera: Jemand")
        db.add(models.UserLike(user_id=user.id, epg_event_id=ev.id, title="Ratatouille",
                               sentiment="like", created_at=utcnow()))
        db.commit()
        likes, _ = _get_recent_reactions(user.id, db)
        assert likes[0]["credits"] == "Regie: Brad Bird"

    def test_reaction_without_a_live_event_has_no_credits(self, db: Session):
        """EPG cleanup deletes old events; the snapshot fields survive it."""
        user = make_user(db)
        db.add(models.UserLike(user_id=user.id, epg_event_id=None, title="Alte Show",
                               sentiment="like", created_at=utcnow()))
        db.commit()
        likes, _ = _get_recent_reactions(user.id, db)
        assert likes[0]["credits"] == ""

    def test_history_carries_the_credits(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        ev = make_event(db, ch, title="Death in Paradise",
                        long_desc="Krimi. Darsteller: Don Gilet - Mervin Wilson Regie: Jemand")
        make_session(db, user, ch, epg_event=ev)
        db.commit()
        history = _get_recent_history(user.id, db)
        assert history[0]["credits"] == "Regie: Jemand Darsteller: Don Gilet - Mervin Wilson"

    def test_history_only_confirmed_recent(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        make_session(db, user, ch, confirmed=True, days_ago=1)
        make_session(db, user, ch, confirmed=False, days_ago=1)
        make_session(db, user, ch, confirmed=True, days_ago=40)  # outside 30d window
        db.commit()
        history = _get_recent_history(user.id, db)
        assert len(history) == 1


# ── on-demand re-rate window ──────────────────────────────────────────────────

class TestWindowEventIds:
    def test_window_is_now_until_plus_hours(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        make_event(db, ch, title="läuft", offset_min=-30)                 # on air
        make_event(db, ch, title="gleich", offset_min=60)                 # in window
        make_event(db, ch, title="spaeter", offset_min=60 * 10)           # outside
        make_event(db, ch, title="vorbei", offset_min=-600,
                   duration_sec=60)                                       # already over
        with patch("app.services.scoring.get_channels_for_user", return_value=[ch]):
            ids = _window_event_ids(user.id, 4.0, db)
        titles = {db.get(models.EpgEvent, i).title for i in ids}
        assert titles == {"läuft", "gleich"}

    def test_other_channels_are_not_in_the_window(self, db: Session):
        user = make_user(db)
        mine = make_channel(db, sref="1:0:1:1:1:1:0:0:0:0:", name="Meins")
        other = make_channel(db, sref="1:0:2:2:2:2:0:0:0:0:", name="Fremd")
        make_event(db, mine, title="meins", offset_min=30)
        make_event(db, other, title="fremd", offset_min=30)
        with patch("app.services.scoring.get_channels_for_user", return_value=[mine]):
            ids = _window_event_ids(user.id, 4.0, db)
        assert [db.get(models.EpgEvent, i).title for i in ids] == ["meins"]

    def test_no_visible_channel_means_no_window(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        make_event(db, ch, offset_min=30)
        with patch("app.services.scoring.get_channels_for_user", return_value=[]):
            assert _window_event_ids(user.id, 4.0, db) == []


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
