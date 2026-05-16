"""Parse raw OpenWebif JSON responses into typed dicts."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape


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
    if not s:
        return None
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
    bouquets = []
    for i, svc in enumerate(raw.get("services", [])):
        bref = svc.get("servicereference", "")
        bname = svc.get("servicename", "")
        channels = []
        for j, sub in enumerate(svc.get("subservices", [])):
            sref = sub.get("servicereference", "")
            if not sref or sref.startswith("1:64:"):  # skip markers
                continue
            channels.append(ParsedChannel(
                sref=sref,
                name=_clean(sub.get("servicename", "")) or "",
                position=j,
            ))
        bouquets.append(ParsedBouquet(bref=bref, name=bname, position=i, channels=channels))
    return bouquets


def _ts_to_utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def parse_epg_events(raw: dict) -> list[ParsedEpgEvent]:
    """Works for epgnow, epgnext, and epgservice responses."""
    events = []
    for e in raw.get("events", []):
        begin_ts = e.get("begin_timestamp", 0)
        dur = e.get("duration_sec", 0)
        if not begin_ts or not dur:
            continue
        title = _clean(e.get("title", "")) or ""
        if title in ("N/A", "n/a", ""):
            continue  # freshly booted box, skip placeholder events
        # Genre: openATV/OWIF 2.2+ provides human-readable German string
        genre = _clean(e.get("genre")) or None

        events.append(ParsedEpgEvent(
            sref=e.get("sref", ""),
            channel_name=_clean(e.get("sname", "")) or "",
            event_id=e.get("id"),
            title=title,
            short_desc=_clean(e.get("shortdesc")),
            long_desc=_clean(e.get("longdesc")),
            start_time=_ts_to_utc(begin_ts),
            end_time=_ts_to_utc(begin_ts + dur),
            duration_sec=dur,
            genre=genre,
        ))
    return events


def parse_current(raw: dict) -> ParsedCurrentService | None:
    """Parse getcurrent response. Returns None if receiver is idle/standby."""
    info = raw.get("info", {})
    sref = info.get("ref", "")
    if not sref or not info.get("result"):
        return None
    now_event = raw.get("now", {})
    return ParsedCurrentService(
        sref=sref,
        name=info.get("name", ""),
        event_id=now_event.get("id"),
        title=now_event.get("title"),
    )
