from datetime import date

import pytest

from tests.fakes import FakeYouTubeAPI
from youtube_studio_mcp.analytics import (
    add_shares,
    aggregate_daily_rows,
    aggregate_totals,
    join_filters,
    label_for,
    pct_change,
    run_report,
    run_report_with_optional_metrics,
    video_filter,
    weight_metrics_needed,
)
from youtube_studio_mcp.periods import DateRange

RANGE = DateRange(date(2026, 1, 28), date(2026, 2, 3), "custom")


def daily(day: str, views: int, avd: float, minutes: int = 0) -> dict[str, object]:
    return {"day": day, "views": views, "averageViewDuration": avd, "estimatedMinutesWatched": minutes}


def test_monthly_aggregation_sums_and_weights() -> None:
    rows = [
        daily("2026-01-30", 100, 60, 100),
        daily("2026-01-31", 300, 120, 600),
        daily("2026-02-01", 50, 30, 25),
    ]
    out = aggregate_daily_rows(
        rows, ["views", "averageViewDuration", "estimatedMinutesWatched"], "month", date_range=RANGE
    )
    assert [r["period"] for r in out] == ["2026-01", "2026-02"]
    january = out[0]
    assert january["views"] == 400
    assert january["estimatedMinutesWatched"] == 700
    assert january["averageViewDuration"] == pytest.approx((100 * 60 + 300 * 120) / 400)
    assert january["partial"] is True  # range starts on the 28th
    assert out[1]["partial"] is True


def test_weekly_buckets_start_monday() -> None:
    rows = [daily("2026-02-01", 10, 1), daily("2026-02-02", 20, 1)]  # Sunday, Monday
    out = aggregate_daily_rows(rows, ["views"], "week", date_range=RANGE)
    assert [r["period"] for r in out] == ["2026-01-26", "2026-02-02"]


def test_unknown_metrics_cannot_be_aggregated() -> None:
    with pytest.raises(ValueError, match="Cannot aggregate"):
        aggregate_daily_rows([{"day": "2026-02-01", "uniques": 3}], ["uniques"], "month", date_range=RANGE)


def test_aggregate_totals_by_group() -> None:
    rows = [
        {"t": "A", "views": 10, "averageViewDuration": 10},
        {"t": "A", "views": 30, "averageViewDuration": 30},
        {"t": "B", "views": 0, "averageViewDuration": 99},
    ]
    out = aggregate_totals(rows, ["views", "averageViewDuration"], group_by="t")
    assert out["A"] == {"views": 40, "averageViewDuration": 25.0}
    assert out["B"]["averageViewDuration"] is None  # no views to weight by


def test_helpers() -> None:
    rows = [{"v": 30}, {"v": 10}, {"v": 0}]
    add_shares(rows, "v")
    assert [r["share_pct"] for r in rows] == [75.0, 25.0, 0.0]
    assert pct_change(110, 100) == 10.0
    assert pct_change(5, 0) is None
    assert pct_change(None, 3) is None
    assert video_filter(["a", " b "]) == "video==a,b"
    assert video_filter(None) is None
    assert join_filters("a==1", None, "b==2") == "a==1;b==2"
    assert weight_metrics_needed(["averageViewDuration", "cpm", "views"]) == ["adImpressions"]
    assert label_for("insightTrafficSourceType", "YT_SEARCH") == "YouTube search"
    assert label_for("insightTrafficSourceType", "NEW_THING") == "New Thing"
    assert label_for("ageGroup", "age25-34") == "25-34"
    assert label_for("country", "US") is None


def test_run_report_builds_query_and_rows() -> None:
    api = FakeYouTubeAPI()
    report = run_report(api, RANGE, ["views", "likes"], dimensions=["day"], sort="day", currency="eur")
    _, params = api.calls[-1]
    assert params["startDate"] == "2026-01-28"
    assert params["currency"] == "EUR"
    assert report.columns == ["day", "views", "likes"]
    assert len(report.rows) == 7
    assert report.rows[0]["day"] == "2026-01-28"


def test_run_report_rejects_bad_names() -> None:
    with pytest.raises(ValueError, match="Invalid metric"):
        run_report(FakeYouTubeAPI(), RANGE, ["views;drop"])
    with pytest.raises(ValueError, match="At least one metric"):
        run_report(FakeYouTubeAPI(), RANGE, [])


def test_optional_metrics_fall_back_on_400() -> None:
    api = FakeYouTubeAPI(engaged_views_supported=False)
    report = run_report_with_optional_metrics(api, RANGE, ["views"], ["engagedViews"])
    assert report.columns == ["views"]
    assert len([c for c in api.calls if c[0] == "analytics"]) == 2
