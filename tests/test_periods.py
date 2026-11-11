from datetime import date

import pytest

from youtube_studio_mcp.periods import DateRange, PeriodError, add_months, data_lag_days, resolve_period

TODAY = date(2026, 11, 11)


def resolve(period: str | None = None, start: str | None = None, end: str | None = None) -> DateRange:
    return resolve_period(period, start, end, today=TODAY, channel_start=lambda: date(2019, 3, 2))


def test_rolling_periods_end_at_latest_complete_day() -> None:
    r = resolve("last_28_days")
    assert r.end == date(2026, 11, 8)  # default lag: 3 days
    assert r.days == 28
    assert r.start == date(2026, 10, 12)


def test_lag_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_STUDIO_MCP_DATA_LAG_DAYS", "1")
    assert data_lag_days() == 1
    assert resolve("last_7_days").end == date(2026, 11, 10)
    monkeypatch.setenv("YOUTUBE_STUDIO_MCP_DATA_LAG_DAYS", "99")
    assert data_lag_days() == 7


@pytest.mark.parametrize(
    ("period", "start", "end"),
    [
        ("this_month", date(2026, 11, 1), date(2026, 11, 8)),
        ("last_month", date(2026, 10, 1), date(2026, 10, 31)),
        ("this_year", date(2026, 1, 1), date(2026, 11, 8)),
        ("last_year", date(2025, 1, 1), date(2025, 12, 31)),
        ("all_time", date(2019, 3, 2), date(2026, 11, 8)),
        ("lifetime", date(2019, 3, 2), date(2026, 11, 8)),
    ],
)
def test_named_periods(period: str, start: date, end: date) -> None:
    r = resolve(period)
    assert (r.start, r.end) == (start, end)


def test_custom_dates_override_period() -> None:
    r = resolve("last_7_days", "2026-01-01", "2026-01-31")
    assert (r.start, r.end, r.label) == (date(2026, 1, 1), date(2026, 1, 31), "custom")
    assert resolve(None, "2026-10-01").end == date(2026, 11, 8)


@pytest.mark.parametrize(
    ("period", "start", "end", "message"),
    [
        ("nope", None, None, "Unknown period"),
        (None, None, "2026-01-01", "without start_date"),
        (None, "2026-13-01", None, "YYYY-MM-DD"),
        (None, "2004-01-01", None, "earlier than"),
        (None, "2026-02-01", "2026-01-01", "after end date"),
    ],
)
def test_invalid_input(period: str | None, start: str | None, end: str | None, message: str) -> None:
    with pytest.raises(PeriodError, match=message):
        resolve(period, start, end)


def test_previous_period_has_equal_length() -> None:
    r = resolve("last_28_days")
    prev = r.previous()
    assert prev.days == 28
    assert prev.end == date(2026, 10, 11)


def test_month_alignment_and_add_months() -> None:
    r = DateRange(date(2026, 2, 10), date(2026, 4, 3), "x").month_aligned()
    assert (r.start, r.end) == (date(2026, 2, 1), date(2026, 4, 30))
    assert add_months(date(2026, 1, 1), -13) == date(2024, 12, 1)
    assert add_months(date(2026, 11, 1), 2) == date(2027, 1, 1)
