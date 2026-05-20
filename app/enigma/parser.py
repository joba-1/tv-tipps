"""Parse raw OpenWebif JSON responses into typed dicts.

Each parser is defensive against malformed payloads from a misbehaving or
hostile receiver: individual bad events are dropped and counted, structural
failures bubble up as ``EnigmaParseError`` so the caller can dump+skip the
batch instead of aborting an entire refresh."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from app.logging_setup import get_logger

log = get_logger(__name__)

# Per-field safety caps. Receivers should never produce values close to these;
# anything bigger is corruption or a hostile payload. Stops a single bogus row
# from filling the SQLite DB.
_MAX_STR_LEN = 8_000
# IPTV streaming bouquets (Pluto TV, Samsung TV Plus, Rakuten TV...) embed the
# full HLS URL inside the sref, so legitimate refs can run up to ~500 chars.
_MAX_REF_LEN = 1024
_MAX_EVENTS_PER_RESPONSE = 5_000


class EnigmaParseError(Exception):
    """Raised when an OpenWebif payload is structurally unusable."""


_DVB_C1 = str.maketrans({
    # EN 300 468 Annex A control characters
    "\x80": "€",   # cp1252 0x80 = € (euro sign); OpenWebif misreads it as U+0080
    "\x86": "",    # Emphasis On  — no plain-text equivalent, drop
    "\x87": "",    # Emphasis Off — no plain-text equivalent, drop
    "\x8a": "\n",  # CR/LF line separator
    "\x1a": "\n",  # field separator used by some German broadcasters
})


def _clean(s: str | None) -> str | None:
    """Unescape HTML entities and normalise DVB/ISO 6937 control characters."""
    if s is None:
        return None
    if not isinstance(s, str):
        # Receiver returned int/list/None where a string was expected — drop
        # rather than coerce; downstream stringification would mask a bug.
        return None
    if not s:
        return None
    if len(s) > _MAX_STR_LEN:
        s = s[:_MAX_STR_LEN]
    s = unescape(s)
    s = s.translate(_DVB_C1)
    # strip any remaining C0/C1 control chars not listed above
    s = "".join(ch for ch in s if ord(ch) >= 32 or ch in "\n\t")
    return s or None


@dataclass
class ParsedBouquet:
    bref: str
    name: str
    position: int
    channels: list["ParsedChannel"]


@dataclass
class ParsedChannel:
    sref: str
    name: str
    picon_path: str | None = None
    position: int = 0


@dataclass
class ParsedEpgEvent:
    sref: str
    channel_name: str
    event_id: int | None
    title: str
    short_desc: str | None
    long_desc: str | None
    start_time: datetime   # naive UTC
    end_time: datetime     # naive UTC
    duration_sec: int
    genre: str | None = None


@dataclass
class ParsedCurrentService:
    sref: str
    name: str
    event_id: int | None
    title: str | None


def parse_all_services(raw: dict) -> list[ParsedBouquet]:
    if not isinstance(raw, dict):
        raise EnigmaParseError(f"expected dict, got {type(raw).__name__}")
    services = raw.get("services", [])
    if not isinstance(services, list):
        raise EnigmaParseError(f"services: expected list, got {type(services).__name__}")
    bouquets, dropped = [], 0
    for i, svc in enumerate(services):
        if not isinstance(svc, dict):
            dropped += 1
            continue
        bref = svc.get("servicereference", "")
        bname = svc.get("servicename", "")
        if not isinstance(bref, str) or not isinstance(bname, str) or len(bref) > _MAX_REF_LEN:
            dropped += 1
            continue
        channels = []
        for j, sub in enumerate(svc.get("subservices", []) or []):
            if not isinstance(sub, dict):
                continue
            sref = sub.get("servicereference", "")
            if not isinstance(sref, str) or not sref or sref.startswith("1:64:") or len(sref) > _MAX_REF_LEN:
                continue
            channels.append(ParsedChannel(
                sref=sref,
                name=_clean(sub.get("servicename", "")) or "",
                position=j,
            ))
        bouquets.append(ParsedBouquet(bref=bref, name=bname, position=i, channels=channels))
    if dropped:
        log.warning("enigma.bouquet_dropped", count=dropped, total=len(services))
    return bouquets


def _ts_to_utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def parse_epg_events(raw: dict) -> list[ParsedEpgEvent]:
    """Works for epgnow, epgnext, and epgservice responses."""
    if not isinstance(raw, dict):
        raise EnigmaParseError(f"expected dict, got {type(raw).__name__}")
    raw_events = raw.get("events", [])
    if not isinstance(raw_events, list):
        raise EnigmaParseError(f"events: expected list, got {type(raw_events).__name__}")
    if len(raw_events) > _MAX_EVENTS_PER_RESPONSE:
        raise EnigmaParseError(
            f"events: {len(raw_events)} exceeds cap {_MAX_EVENTS_PER_RESPONSE}"
        )
    events, dropped = [], 0
    for e in raw_events:
        if not isinstance(e, dict):
            dropped += 1
            continue
        try:
            begin_ts = e.get("begin_timestamp")
            dur = e.get("duration_sec")
            # Silent skip: receiver returns None for channels with no current
            # EPG info (radio, parked channels) — common, not corruption.
            if begin_ts is None or dur is None or begin_ts == 0 or dur == 0:
                continue
            if not isinstance(begin_ts, int) or not isinstance(dur, int):
                dropped += 1
                continue
            if begin_ts < 0 or dur < 0:
                dropped += 1
                continue
            # Sanity cap on duration (one week) — guards _ts_to_utc against
            # overflow and stops absurd timetable rows from polluting the DB.
            if dur > 7 * 24 * 3600:
                dropped += 1
                continue
            title = _clean(e.get("title", "")) or ""
            if title in ("N/A", "n/a", ""):
                continue  # freshly booted box, skip placeholder events
            sref = e.get("sref", "")
            if not isinstance(sref, str) or len(sref) > _MAX_REF_LEN:
                dropped += 1
                continue
            event_id = e.get("id")
            if event_id is not None and not isinstance(event_id, int):
                event_id = None
            genre = _clean(e.get("genre")) or None
            events.append(ParsedEpgEvent(
                sref=sref,
                channel_name=_clean(e.get("sname", "")) or "",
                event_id=event_id,
                title=title,
                short_desc=_clean(e.get("shortdesc")),
                long_desc=_clean(e.get("longdesc")),
                start_time=_ts_to_utc(begin_ts),
                end_time=_ts_to_utc(begin_ts + dur),
                duration_sec=dur,
                genre=genre,
            ))
        except (TypeError, ValueError, OverflowError, OSError):
            dropped += 1
    if dropped:
        log.warning("enigma.epg_event_dropped", count=dropped, total=len(raw_events))
    return events


def parse_current(raw: dict) -> ParsedCurrentService | None:
    """Parse getcurrent response. Returns None if receiver is idle/standby."""
    if not isinstance(raw, dict):
        raise EnigmaParseError(f"expected dict, got {type(raw).__name__}")
    info = raw.get("info") or {}
    if not isinstance(info, dict):
        raise EnigmaParseError(f"info: expected dict, got {type(info).__name__}")
    sref = info.get("ref", "")
    if not isinstance(sref, str) or not sref or len(sref) > _MAX_REF_LEN or not info.get("result"):
        return None
    now_event = raw.get("now") or {}
    if not isinstance(now_event, dict):
        now_event = {}
    event_id = now_event.get("id")
    if event_id is not None and not isinstance(event_id, int):
        event_id = None
    name = info.get("name", "")
    title = now_event.get("title")
    return ParsedCurrentService(
        sref=sref,
        name=name if isinstance(name, str) else "",
        event_id=event_id,
        title=title if isinstance(title, str) else None,
    )
