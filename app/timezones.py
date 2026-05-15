from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from config import settings


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def from_timestamp(ts: int) -> datetime:
    """Unix epoch → naive UTC datetime."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def to_local_str(dt: datetime) -> str:
    """Naive UTC datetime → local time string for display / AI prompts."""
    aware = dt.replace(tzinfo=timezone.utc)
    local = aware.astimezone(_tz())
    return local.strftime("%Y-%m-%d %H:%M")


def tonight_range() -> tuple[datetime, datetime]:
    """Return (start, end) in naive UTC for tonight 18:00 → 02:00 local.
    Start is max(18:00, now) so already-ended shows are excluded."""
    tz = _tz()
    now_local = datetime.now(tz)
    base_start = now_local.replace(hour=18, minute=0, second=0, microsecond=0)
    if now_local.hour < 2:
        base_start -= timedelta(days=1)
    end_local = base_start + timedelta(hours=8)  # always 02:00
    start_local = max(base_start, now_local)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def hours_range(hours: float) -> tuple[datetime, datetime]:
    now = utcnow()
    return now, now + timedelta(hours=hours)


def prime_range() -> tuple[datetime, datetime]:
    """Return (start, end) naive UTC for tonight's prime-time window.
    If we're already past tonight's prime end, return tomorrow's window.
    """
    tz = _tz()
    now_local = datetime.now(tz)
    prime_start = now_local.replace(
        hour=settings.prime_start_hour, minute=0, second=0, microsecond=0
    )
    prime_end = now_local.replace(
        hour=settings.prime_end_hour, minute=0, second=0, microsecond=0
    )
    if prime_end <= now_local:
        prime_start += timedelta(days=1)
        prime_end += timedelta(days=1)
    return (
        prime_start.astimezone(timezone.utc).replace(tzinfo=None),
        prime_end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def today_remaining_range() -> tuple[datetime, datetime]:
    """Return (now, midnight_local) in naive UTC."""
    tz = _tz()
    midnight_local = (
        datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    return utcnow(), midnight_local.astimezone(timezone.utc).replace(tzinfo=None)
