from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def local_run_key(
    now: datetime | None = None,
    *,
    timezone: str = "America/New_York",
) -> str:
    current = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone))
    return f"daily-{current.date().isoformat()}"


def scheduled_run_key(
    now: datetime | None = None,
    *,
    timezone: str = "America/New_York",
    hour: int = 7,
    grace_hours: int = 4,
) -> str | None:
    scheduled_date = _scheduled_date(
        now,
        timezone=timezone,
        hour=hour,
        grace_hours=grace_hours,
    )
    if scheduled_date is None:
        return None
    return f"daily-{scheduled_date.isoformat()}"


def schedule_decision(
    now: datetime,
    *,
    timezone: str,
    hour: int,
    grace_hours: int,
) -> dict[str, str]:
    """Describe the current local day's window without claiming a database run."""
    current = now.astimezone(ZoneInfo(timezone))
    start = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    return {
        "decision_time": current.isoformat(),
        "timezone": timezone,
        "window_start": start.isoformat(),
        "cutoff_exclusive": (start + timedelta(hours=grace_hours)).isoformat(),
        "daily_key": local_run_key(now, timezone=timezone),
    }


def is_scheduled_window(
    now: datetime | None = None,
    *,
    timezone: str = "America/New_York",
    hour: int = 7,
    grace_hours: int = 4,
) -> bool:
    return (
        _scheduled_date(
            now,
            timezone=timezone,
            hour=hour,
            grace_hours=grace_hours,
        )
        is not None
    )


def _scheduled_date(
    now: datetime | None,
    *,
    timezone: str,
    hour: int,
    grace_hours: int,
):
    current = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone))
    target = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if current < target:
        target -= timedelta(days=1)
    if current - target >= timedelta(hours=grace_hours):
        return None
    return target.date()


def notification_delay(now: datetime | None = None, *, timezone: str = "America/New_York") -> float:
    """Wait until today's 09:00 locally; delayed scans deliver immediately."""
    current = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone))
    target = current.replace(hour=9, minute=0, second=0, microsecond=0)
    return max(0.0, target.timestamp() - current.timestamp())
