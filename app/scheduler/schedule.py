"""Pure schedule arithmetic. Times in the DB are timezone-aware UTC; HH:MM in schedules is the host's local time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config.schema import ScheduleSpec

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _to_local(dt: datetime) -> datetime:
    return _aware(dt).astimezone()


def _to_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)


def next_run_after(spec: ScheduleSpec, after_utc: datetime) -> datetime | None:
    """Next due time (aware UTC) strictly after `after_utc`, or None for manual workflows."""
    if spec.kind == "manual":
        return None
    if spec.kind == "interval":
        return _aware(after_utc) + timedelta(hours=float(spec.every_hours or 0))
    local = _to_local(after_utc)
    hh, mm = map(int, (spec.at or "00:00").split(":"))
    candidate = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if spec.kind == "daily":
        if candidate <= local:
            candidate += timedelta(days=1)
        return _to_utc(candidate)
    target = _WEEKDAYS.index(spec.weekday or "mon")
    days_ahead = (target - candidate.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= local:
        candidate += timedelta(days=7)
    return _to_utc(candidate)
