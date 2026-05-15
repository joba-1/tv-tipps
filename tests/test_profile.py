"""Tests for app/services/profile.py — compute, cache, stated preferences."""
from __future__ import annotations
import json
import pytest
from datetime import timedelta
from sqlalchemy.orm import Session
from app.services.profile import compute_profile, get_profile, set_stated_preferences
from app import models
from app.timezones import utcnow
from tests.conftest import make_channel, make_event, make_user, make_session


class TestComputeProfile:
    def test_no_sessions_returns_empty_profile(self, db: Session):
        user = make_user(db)
        db.commit()
        profile = compute_profile(user.id, db)
        assert profile["session_count"] == 0
        assert profile["top_genres"] == []
        assert profile["top_channels"] == []

    def test_session_count_correct(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        make_session(db, user, ch)
        make_session(db, user, ch)
        db.commit()
        profile = compute_profile(user.id, db)
        assert profile["session_count"] == 2

    def test_unconfirmed_sessions_excluded(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        make_session(db, user, ch, confirmed=False)
        db.commit()
        profile = compute_profile(user.id, db)
        assert profile["session_count"] == 0

    def test_genre_counted_correctly(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        make_session(db, user, ch, genre="Action")
        make_session(db, user, ch, genre="Action")
        make_session(db, user, ch, genre="Drama")
        db.commit()
        profile = compute_profile(user.id, db)
        genres = {g["genre"]: g["count"] for g in profile["top_genres"]}
        assert genres["Action"] == 2
        assert genres["Drama"] == 1

    def test_top_genre_first(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        make_session(db, user, ch, genre="Drama")
        make_session(db, user, ch, genre="Action")
        make_session(db, user, ch, genre="Action")
        db.commit()
        profile = compute_profile(user.id, db)
        assert profile["top_genres"][0]["genre"] == "Action"

    def test_channel_counted(self, db: Session):
        user = make_user(db)
        ch = make_channel(db, name="ARD")
        make_session(db, user, ch)
        db.commit()
        profile = compute_profile(user.id, db)
        ch_names = [c["name"] for c in profile["top_channels"]]
        assert "ARD" in ch_names

    def test_avg_duration_computed(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        make_session(db, user, ch, duration_sec=3600)
        make_session(db, user, ch, duration_sec=7200)
        db.commit()
        profile = compute_profile(user.id, db)
        assert profile["avg_duration_min"] == pytest.approx(90.0, abs=0.5)

    def test_profile_persisted_in_db(self, db: Session):
        user = make_user(db)
        db.commit()
        compute_profile(user.id, db)
        saved = db.get(models.UserProfile, user.id)
        assert saved is not None
        data = json.loads(saved.summary_json)
        assert "session_count" in data

    def test_stated_preferences_preserved_on_recompute(self, db: Session):
        user = make_user(db)
        db.commit()
        # First: store stated prefs
        set_stated_preferences(user.id, "Action only", db)
        # Recompute should preserve them
        profile = compute_profile(user.id, db)
        assert profile["stated_preferences"] == "Action only"

    def test_sessions_outside_30_day_window_excluded(self, db: Session):
        user = make_user(db)
        ch = make_channel(db)
        make_session(db, user, ch, days_ago=31)
        db.commit()
        profile = compute_profile(user.id, db)
        assert profile["session_count"] == 0


class TestGetProfile:
    def test_returns_profile(self, db: Session):
        user = make_user(db)
        db.commit()
        profile = get_profile(user.id, db)
        assert isinstance(profile, dict)
        assert "session_count" in profile

    def test_returns_cached_within_24h(self, db: Session):
        user = make_user(db)
        db.commit()
        # Compute once to populate cache
        p1 = get_profile(user.id, db)
        # Add a session — should NOT appear in cached result
        ch = make_channel(db)
        make_session(db, user, ch)
        db.commit()
        p2 = get_profile(user.id, db)
        assert p2["session_count"] == p1["session_count"]

    def test_recomputes_when_stale(self, db: Session):
        user = make_user(db)
        db.commit()
        # Force stale cache by backdating computed_at
        compute_profile(user.id, db)
        saved = db.get(models.UserProfile, user.id)
        saved.computed_at = utcnow() - timedelta(hours=25)
        db.commit()
        ch = make_channel(db)
        make_session(db, user, ch)
        db.commit()
        profile = get_profile(user.id, db)
        assert profile["session_count"] == 1


class TestSetStatedPreferences:
    def test_creates_profile_if_none(self, db: Session):
        user = make_user(db)
        db.commit()
        set_stated_preferences(user.id, "Sci-Fi", db)
        saved = db.get(models.UserProfile, user.id)
        assert saved is not None
        data = json.loads(saved.summary_json)
        assert data["stated_preferences"] == "Sci-Fi"

    def test_updates_existing_profile(self, db: Session):
        user = make_user(db)
        db.commit()
        set_stated_preferences(user.id, "First", db)
        set_stated_preferences(user.id, "Second", db)
        data = json.loads(db.get(models.UserProfile, user.id).summary_json)
        assert data["stated_preferences"] == "Second"

    def test_empty_string_allowed(self, db: Session):
        user = make_user(db)
        db.commit()
        set_stated_preferences(user.id, "", db)
        data = json.loads(db.get(models.UserProfile, user.id).summary_json)
        assert data["stated_preferences"] == ""
