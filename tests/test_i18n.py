"""Tests for app/services/i18n.py — normalize, merge priority, missing-key detection."""
import pytest
from sqlalchemy.orm import Session
from app.services.i18n import normalize, get_translations
from app import models
from app.timezones import utcnow


class TestNormalize:
    def test_simple_code(self):
        assert normalize("de") == "de"
        assert normalize("en") == "en"

    def test_strips_region(self):
        assert normalize("en-US") == "en"
        assert normalize("de-DE") == "de"
        assert normalize("zh-Hans-CN") == "zh"

    def test_uppercased_input(self):
        assert normalize("DE") == "de"
        assert normalize("EN-GB") == "en"

    def test_whitespace(self):
        assert normalize("  de ") == "de"

    def test_empty_string(self):
        assert normalize("") == ""

    def test_numeric_invalid(self):
        assert normalize("123") == ""

    def test_too_long(self):
        assert normalize("toolong") == ""

    def test_one_char_invalid(self):
        assert normalize("x") == ""

    def test_three_char_valid(self):
        # ISO 639-2 codes are 3 chars
        assert normalize("zho") == "zho"


class TestGetTranslations:
    def test_german_returns_source_no_missing(self, db: Session):
        strings, missing = get_translations("de", db)
        assert isinstance(strings, dict)
        assert len(strings) > 0
        assert missing == []

    def test_german_has_nav_keys(self, db: Session):
        strings, _ = get_translations("de", db)
        assert "nav.tips" in strings
        assert "btn.watch" in strings
        assert "msg.cold_start" in strings

    def test_english_curated_no_missing(self, db: Session):
        # en.json exists as a curated file → nothing should be missing
        strings, missing = get_translations("en", db)
        assert missing == []
        assert strings.get("btn.close") == "Close"

    def test_unknown_lang_all_missing(self, db: Session):
        strings, missing = get_translations("xx", db)
        # Falls back to German source for all values
        src_keys = set(get_translations("de", db)[0].keys())
        assert set(missing) == src_keys

    def test_unknown_lang_values_are_german_fallback(self, db: Session):
        strings, _ = get_translations("xx", db)
        de_strings, _ = get_translations("de", db)
        assert strings == de_strings

    def test_db_ai_translation_overrides_fallback(self, db: Session):
        now = utcnow()
        db.add(models.Translation(
            lang="fr", key="btn.close", value="Fermer",
            source="ai", generated_at=now,
        ))
        db.commit()
        strings, missing = get_translations("fr", db)
        assert strings["btn.close"] == "Fermer"
        assert "btn.close" not in missing

    def test_curated_overrides_db_ai(self, db: Session):
        # Even if DB has a value for an English key, the curated en.json wins
        now = utcnow()
        db.add(models.Translation(
            lang="en", key="btn.close", value="AI-Close",
            source="ai", generated_at=now,
        ))
        db.commit()
        strings, _ = get_translations("en", db)
        # Curated en.json has "Close", not "AI-Close"
        assert strings["btn.close"] == "Close"

    def test_missing_only_non_curated_keys(self, db: Session):
        # For a lang with partial DB coverage, only non-covered keys are missing
        now = utcnow()
        db.add(models.Translation(
            lang="es", key="btn.close", value="Cerrar",
            source="ai", generated_at=now,
        ))
        db.commit()
        _, missing = get_translations("es", db)
        assert "btn.close" not in missing
        assert "btn.watch" in missing  # not in DB for "es"
