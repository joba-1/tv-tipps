"""Tests for app/enigma/parser.py — uses real fixture files."""
import json
from pathlib import Path
import pytest
from app.enigma.parser import parse_epg_events, parse_all_services, parse_current

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


# ── parse_epg_events ──────────────────────────────────────────────────────────

class TestParseEpgEvents:
    def test_epgnow_returns_events(self):
        events = parse_epg_events(_load("epgnow"))
        assert len(events) > 0

    def test_epgnow_event_fields(self):
        events = parse_epg_events(_load("epgnow"))
        ev = events[0]
        assert ev.title and ev.title not in ("N/A", "")
        assert ev.sref
        assert ev.start_time < ev.end_time
        assert ev.duration_sec > 0

    def test_epgnext_events(self):
        events = parse_epg_events(_load("epgnext"))
        assert len(events) > 0
        ev = events[0]
        assert ev.start_time < ev.end_time

    def test_epgservice_events(self):
        events = parse_epg_events(_load("epgservice"))
        assert len(events) >= 3  # sparse guard threshold

    def test_duration_matches_start_end(self):
        events = parse_epg_events(_load("epgnow"))
        for ev in events:
            expected = int((ev.end_time - ev.start_time).total_seconds())
            assert abs(expected - ev.duration_sec) <= 1

    def test_html_entities_unescaped(self):
        # Titles/descs from OWIF sometimes contain &amp; etc.
        events = parse_epg_events(_load("epgnow"))
        for ev in events:
            assert "&amp;" not in (ev.title or "")
            assert "&amp;" not in (ev.short_desc or "")

    def test_na_titles_skipped(self):
        raw = {"events": [
            {"sref": "1:0:1:1:1:1:0:0:0:0:", "title": "N/A",
             "begin_timestamp": 1700000000, "duration_sec": 3600},
            {"sref": "1:0:1:1:1:1:0:0:0:0:", "title": "Real Show",
             "begin_timestamp": 1700003600, "duration_sec": 1800},
        ]}
        events = parse_epg_events(raw)
        assert len(events) == 1
        assert events[0].title == "Real Show"

    def test_missing_duration_skipped(self):
        raw = {"events": [
            {"sref": "1:0:1:1:1:1:0:0:0:0:", "title": "No Duration",
             "begin_timestamp": 1700000000, "duration_sec": 0},
        ]}
        assert parse_epg_events(raw) == []

    def test_box17_epgnow(self):
        events = parse_epg_events(_load("epgnow_box17"))
        assert len(events) > 0

    def test_datetimes_are_naive_utc(self):
        events = parse_epg_events(_load("epgnow"))
        for ev in events:
            assert ev.start_time.tzinfo is None
            assert ev.end_time.tzinfo is None


# ── parse_all_services ────────────────────────────────────────────────────────

class TestParseAllServices:
    def test_returns_bouquets(self):
        bouquets = parse_all_services(_load("getallservices"))
        assert len(bouquets) > 0

    def test_bouquet_has_channels(self):
        bouquets = parse_all_services(_load("getallservices"))
        total_channels = sum(len(b.channels) for b in bouquets)
        assert total_channels > 0

    def test_markers_excluded(self):
        # Markers have sref starting with 1:64:
        bouquets = parse_all_services(_load("getallservices"))
        for b in bouquets:
            for ch in b.channels:
                assert not ch.sref.startswith("1:64:")

    def test_box17_services(self):
        bouquets = parse_all_services(_load("getallservices_box17"))
        assert len(bouquets) > 0

    def test_channel_has_name_and_sref(self):
        bouquets = parse_all_services(_load("getallservices"))
        for b in bouquets:
            for ch in b.channels:
                assert ch.sref
                assert ch.name


# ── parse_current ─────────────────────────────────────────────────────────────

class TestParseCurrent:
    def test_getcurrent_standby_returns_none(self):
        # Both fixture files capture a receiver in standby (result=false, empty ref)
        result = parse_current(_load("getcurrent"))
        assert result is None

    def test_box17_standby_returns_none(self):
        result = parse_current(_load("getcurrent_box17"))
        assert result is None

    def test_active_receiver_returns_service(self):
        raw = {
            "info": {"ref": "1:0:1:2EF4:441:1:C00000:0:0:0:", "name": "RTL", "result": True},
            "now": {"id": 42, "title": "Some Show"},
        }
        result = parse_current(raw)
        assert result is not None
        assert result.sref == "1:0:1:2EF4:441:1:C00000:0:0:0:"
        assert result.name == "RTL"
        assert result.event_id == 42
