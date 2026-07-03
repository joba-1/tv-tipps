"""Pydantic response/request models."""
from __future__ import annotations
from pydantic import BaseModel, model_validator


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
    # Only populated for events whose score for the current user is "good"
    # (≥ 0.7). Lower scores → null so the UI can choose to show no badge.
    match_score: float | None = None


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
    event_id: int | None = None
    channel_id: int
    channel_name: str
    sref: str
    title: str
    short_desc: str | None
    long_desc: str | None = None
    start_time: str
    end_time: str
    duration_sec: int | None
    genre: str | None = None
    progress_pct: float
    # Only populated for events with a good score (≥ 0.7) for the current user.
    match_score: float | None = None


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
    default_user: str | None = None
    wol_mac: str | None = None
    intertechno_family: str = ""
    intertechno_device: int = 1
    intertechno_url: str = ""
    # OpenWebif newstate for light standby — firmware-specific (VTi=4, openATV=5)
    standby_newstate: int = 4


class AdminStatus(BaseModel):
    receivers: list[ReceiverStatus]
    db_channel_count: int
    db_epg_event_count: int


class RecommendationItem(BaseModel):
    sref: str | None = None
    event_id: int | None = None
    channel_name: str | None = None
    title: str
    short_desc: str | None = None
    long_desc: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    genre: str | None = None
    progress_pct: float = 0.0
    match_score: float = 0.5
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: object) -> object:
        if isinstance(data, dict):
            # Some models return "score" instead of "match_score"
            if "score" in data and "match_score" not in data:
                data["match_score"] = data.pop("score")
        return data


class RecommendationResponse(BaseModel):
    taste_summary: str = ""
    recommendations: list[RecommendationItem]
