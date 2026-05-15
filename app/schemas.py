"""Pydantic response/request models."""
from __future__ import annotations
from pydantic import BaseModel


class EpgEventOut(BaseModel):
    id: int
    event_id: int | None
    title: str
    short_desc: str | None
    long_desc: str | None
    start_time: str
    end_time: str
    duration_sec: int | None
    genre: str | None
    progress_pct: float


class NowNextOut(BaseModel):
    channel_id: int
    channel_name: str
    sref: str
    picon_path: str | None
    stale: bool
    now: EpgEventOut | None
    next: EpgEventOut | None


class EpgRangeItem(BaseModel):
    id: int
    channel_id: int
    channel_name: str
    sref: str
    title: str
    short_desc: str | None
    start_time: str
    end_time: str
    duration_sec: int | None
    genre: str | None = None
    progress_pct: float


class ReceiverStatus(BaseModel):
    name: str
    ip: str
    online: bool
    power_state: str
    last_seen: str | None
    epg_cached_at: str | None
    power_method: str = "none"
    has_genre: bool = False
    priority: int = 99
    location: str = ""


class AdminStatus(BaseModel):
    receivers: list[ReceiverStatus]
    db_channel_count: int
    db_epg_event_count: int


class RecommendationItem(BaseModel):
    sref: str
    channel_name: str
    title: str
    start_time: str
    end_time: str
    genre: str | None = None
    match_score: float
    reason: str


class RecommendationResponse(BaseModel):
    taste_summary: str
    recommendations: list[RecommendationItem]
