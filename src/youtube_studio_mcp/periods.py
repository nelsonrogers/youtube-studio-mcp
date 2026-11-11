"""Date ranges for analytics queries.

YouTube Analytics reports use dates in US Pacific time, and the most recent 2-3 days are
usually incomplete. Rolling periods therefore end ``data_lag_days()`` (default 3) before "today"
(Pacific) so every day in the range is complete; override with
``YOUTUBE_STUDIO_MCP_DATA_LAG_DAYS`` (0-7).
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from youtube_studio_mcp.config import env

PACIFIC = ZoneInfo("America/Los_Angeles")
EARLIEST_DATE = date(2005, 2, 14)  # YouTube's launch; the API rejects earlier dates.

Period = Literal[
    "last_7_days",
    "last_28_days",
    "last_90_days",
    "last_365_days",
    "this_month",
    "last_month",
    "this_year",
    "last_year",
    "all_time",
]
PERIODS: tuple[str, ...] = (
    "last_7_days",
    "last_28_days",
    "last_90_days",
    "last_365_days",
    "this_month",
    "last_month",
    "this_year",
    "last_year",
    "all_time",
)
_ROLLING = {"last_7_days": 7, "last_28_days": 28, "last_90_days": 90, "last_365_days": 365}


class PeriodError(ValueError):
    """Invalid period or date input (message is user-facing)."""


def data_lag_days() -> int:
    raw = env("DATA_LAG_DAYS")
    if raw is None:
        return 3
    try:
        value = int(raw)
    except ValueError as exc:
        raise PeriodError(f"YOUTUBE_STUDIO_MCP_DATA_LAG_DAYS must be an integer, got {raw!r}") from exc
    return max(0, min(7, value))


def today_pacific() -> date:
    return datetime.now(PACIFIC).date()


def parse_date(value: str, field_name: str) -> date:
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise PeriodError(f"{field_name} must be a date in YYYY-MM-DD format, got {value!r}.") from exc
    if parsed < EARLIEST_DATE:
        raise PeriodError(f"{field_name} cannot be earlier than {EARLIEST_DATE.isoformat()}.")
    return parsed


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def add_months(day: date, months: int) -> date:
    month_index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(month_index, 12)
    return date(year, month + 1, 1)


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
    label: str

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise PeriodError(
                f"Start date {self.start.isoformat()} is after end date {self.end.isoformat()}."
            )

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> DateRange:
        """The period of equal length immediately before this one (for comparisons)."""
        end = self.start - timedelta(days=1)
        start = end - timedelta(days=self.days - 1)
        if start < EARLIEST_DATE:
            start = EARLIEST_DATE
        return DateRange(start, max(start, end), f"previous {self.days} days")

    def month_aligned(self) -> DateRange:
        """Expanded to whole calendar months (required by the ``month`` dimension)."""
        return DateRange(self.start.replace(day=1), month_end(self.end), self.label)

    def to_dict(self) -> dict[str, str | int]:
        return {
            "start_date": self.start.isoformat(),
            "end_date": self.end.isoformat(),
            "days": self.days,
            "label": self.label,
        }


def resolve_period(
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    channel_start: Callable[[], date] | None = None,
    today: date | None = None,
    default: str = "last_28_days",
) -> DateRange:
    """Resolve a named period or explicit dates into a concrete :class:`DateRange`.

    Explicit ``start_date``/``end_date`` win over ``period``. ``end_date`` defaults to the
    latest complete day. ``all_time`` needs ``channel_start`` (the channel creation date).
    """
    latest = (today or today_pacific()) - timedelta(days=data_lag_days())

    if start_date or end_date:
        if not start_date:
            raise PeriodError("end_date was given without start_date; provide both, or use `period`.")
        start = parse_date(start_date, "start_date")
        end = parse_date(end_date, "end_date") if end_date else latest
        return DateRange(start, end, "custom")

    name = (period or default).strip().lower()
    if name in _ROLLING:
        days = _ROLLING[name]
        return DateRange(latest - timedelta(days=days - 1), latest, name)
    if name == "this_month":
        return DateRange(latest.replace(day=1), latest, name)
    if name == "last_month":
        first_of_this_month = latest.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        return DateRange(last_month_end.replace(day=1), last_month_end, name)
    if name == "this_year":
        return DateRange(date(latest.year, 1, 1), latest, name)
    if name == "last_year":
        return DateRange(date(latest.year - 1, 1, 1), date(latest.year - 1, 12, 31), name)
    if name in {"all_time", "lifetime"}:
        if channel_start is None:
            raise PeriodError("all_time requires the channel creation date.")
        start = max(channel_start(), EARLIEST_DATE)
        return DateRange(min(start, latest), latest, "all_time")
    raise PeriodError(f"Unknown period {period!r}. Use one of: {', '.join(PERIODS)}, or start_date/end_date.")
