"""Timezone-aware request bounds and calendar-day intervals."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .settings import settings


class TimeRangeError(ValueError):
    pass


def parse_bounds(start_text: str, end_text: str, timezone: str) -> tuple[datetime, datetime, ZoneInfo]:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise TimeRangeError("Unknown timezone.") from exc
    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimeRangeError("Start and end must be valid ISO 8601 timestamps.") from exc
    if start.tzinfo is None or end.tzinfo is None:
        raise TimeRangeError("Start and end timestamps must include timezone offsets.")
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    if start_utc >= end_utc:
        raise TimeRangeError("Start must be earlier than end.")
    if end_utc > datetime.now(UTC) + timedelta(minutes=2):
        raise TimeRangeError("End time cannot be more than two minutes in the future.")
    if end_utc - start_utc > timedelta(days=settings.max_range_days):
        raise TimeRangeError(f"The interactive range is limited to {settings.max_range_days} days.")
    return start_utc, end_utc, zone


def choose_step(start: datetime, end: datetime) -> int:
    duration = (end - start).total_seconds()
    desired = max(settings.scrape_interval_seconds, math.ceil(duration / max(1, settings.max_points_per_series - 1)))
    quantum = max(15, settings.scrape_interval_seconds)
    return int(math.ceil(desired / quantum) * quantum)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def daily_intervals(start: datetime, end: datetime, zone: ZoneInfo) -> list[dict[str, object]]:
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    current_day: date = local_start.date()
    final_day: date = local_end.date()
    intervals: list[dict[str, object]] = []
    while current_day <= final_day:
        day_start_local = datetime.combine(current_day, time.min, tzinfo=zone)
        day_end_local = datetime.combine(current_day + timedelta(days=1), time.min, tzinfo=zone)
        interval_start = max(start, day_start_local.astimezone(UTC))
        interval_end = min(end, day_end_local.astimezone(UTC))
        if interval_start < interval_end:
            intervals.append(
                {
                    "local_date": current_day.isoformat(),
                    "start_utc": _iso(interval_start),
                    "end_utc": _iso(interval_end),
                    "start_local": interval_start.astimezone(zone).isoformat(),
                    "end_local": interval_end.astimezone(zone).isoformat(),
                    "duration_seconds": int((interval_end - interval_start).total_seconds()),
                    "partial": interval_start != day_start_local.astimezone(UTC)
                    or interval_end != day_end_local.astimezone(UTC),
                }
            )
        current_day += timedelta(days=1)
    return intervals


def local_offset_label(value: datetime, zone: ZoneInfo) -> str:
    local = value.astimezone(zone)
    offset = local.strftime("%z")
    return f"{local.isoformat()} ({offset[:3]}:{offset[3:]})"
