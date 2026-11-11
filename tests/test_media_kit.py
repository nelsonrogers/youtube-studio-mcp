"""Contract tests for get_media_kit_data: templates consume this JSON verbatim."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from tests.fakes import FakeYouTubeAPI
from tests.test_server import call

pytestmark = pytest.mark.anyio

SAMPLE = Path(__file__).resolve().parent.parent / "docs" / "examples" / "media-kit-data.sample.json"
METRIC = {"value", "previous", "change_pct"}


def shape(value: Any) -> Any:
    """Key structure of a JSON value (lists are represented by their first element)."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0])] if value and isinstance(value[0], dict | list) else []
    return None


@pytest.fixture
async def kit(client: Client) -> dict[str, Any]:
    return await call(client, "get_media_kit_data")


async def test_top_level_contract(kit: dict[str, Any]) -> None:
    assert kit["schema_version"] == "1"
    assert set(kit) == {
        "schema_version",
        "generated_at",
        "data_through",
        "period",
        "comparison_period",
        "channel",
        "highlights",
        "performance",
        "performance_by_content_type",
        "impressions_funnel",
        "daily_trend",
        "monthly_trend",
        "audience",
        "recent_uploads",
        "top_videos",
        "traffic_sources",
        "unavailable",
        "notes",
    }
    assert kit["period"]["label"] == "last_28_days" and kit["period"]["days"] == 28
    assert kit["data_through"] == kit["period"]["end_date"]


async def test_channel_block(kit: dict[str, Any]) -> None:
    channel = kit["channel"]
    assert channel["subscribers"] == 48400 and channel["subscribers_is_rounded"] is True
    assert isinstance(channel["subscribers_from_analytics"], int)
    assert channel["subscribers_from_analytics"] == kit["monthly_trend"][-1]["total_subscribers"]
    assert channel["thumbnail_url"].startswith("https://") and channel["url"].endswith("@mapleandthread")


async def test_performance_blocks(kit: dict[str, Any]) -> None:
    assert set(kit["performance"]) == {
        "views",
        "engaged_views",
        "watch_time_hours",
        "average_view_duration_s",
        "average_view_percentage",
        "subscribers_gained",
        "subscribers_lost",
        "net_subscribers",
        "likes",
        "comments",
        "shares",
        "engagement_rate_pct",
    }
    assert all(set(m) == METRIC for m in kit["performance"].values())
    by_type = kit["performance_by_content_type"]
    assert set(by_type) == {"videos", "shorts"}
    for block in by_type.values():
        assert set(block) == {
            "views",
            "engaged_views",
            "likes",
            "net_subscribers",
            "watch_time_hours",
            "average_view_duration_s",
            "impressions",
            "impressions_ctr_pct",
        }
        assert all(set(m) == METRIC for m in block.values())
        assert block["views"]["value"] > 0 and block["views"]["previous"] > 0
    assert (
        by_type["shorts"]["average_view_duration_s"]["value"]
        < by_type["videos"]["average_view_duration_s"]["value"]
    )
    assert (
        by_type["videos"]["impressions"]["value"] > 0
        and 0 < by_type["videos"]["impressions_ctr_pct"]["value"] < 100
    )


async def test_impressions_funnel(kit: dict[str, Any]) -> None:
    funnel = kit["impressions_funnel"]
    assert funnel["impressions"] > 0
    assert funnel["views_from_impressions"] == round(
        funnel["impressions"] * funnel["impressions_ctr_pct"] / 100
    )
    assert funnel["average_view_duration_s"] is None and funnel["watch_hours_from_impressions"] is None
    assert "impressions_funnel.watch_metrics" in kit["unavailable"]


async def test_daily_trend_covers_every_day(kit: dict[str, Any]) -> None:
    days = kit["daily_trend"]
    assert len(days) == 28
    assert days[0]["date"] == kit["period"]["start_date"] and days[-1]["date"] == kit["period"]["end_date"]
    for day in days:
        assert set(day) == {"date", "views", "views_videos", "views_shorts", "views_live", "views_other"}
        assert (
            day["views"] == day["views_videos"] + day["views_shorts"] + day["views_live"] + day["views_other"]
        )
        assert day["views_other"] > 0  # the fake, like real channels, has views on posts


async def test_audience_blocks(kit: dict[str, Any]) -> None:
    audience = kit["audience"]
    assert [a["label"] for a in audience["age"]] == [
        "13-17",
        "18-24",
        "25-34",
        "35-44",
        "45-54",
        "55-64",
        "65+",
    ]
    assert [g["code"] for g in audience["gender"]] == ["female", "male", "user_specified"]
    assert sum(a["viewer_pct"] for a in audience["age"]) == pytest.approx(100, abs=0.5)
    countries = audience["countries"]
    assert len(countries) == 8 and countries[0] == {
        "code": "US",
        "views": countries[0]["views"],
        "share_pct": countries[0]["share_pct"],
    }
    assert {d["code"] for d in audience["devices"]} >= {"MOBILE", "TV"}
    # Real API values are camelCase (shorts, videoOnDemand, posts); the contract uses uppercase codes.
    assert {c["code"] for c in audience["content_type"]} == {
        "SHORTS",
        "VIDEO_ON_DEMAND",
        "LIVE_STREAM",
        "POSTS",
    }


async def test_uploads_top_videos_and_traffic(kit: dict[str, Any]) -> None:
    uploads = kit["recent_uploads"]
    assert uploads["min_age_days"] == 7
    items = uploads["videos"]["items"]
    assert len(items) == 10
    assert {"title", "views", "published_at", "thumbnail_url", "duration_s"} <= set(items[0])
    assert [i["published_at"] for i in items] == sorted((i["published_at"] for i in items), reverse=True)
    assert all(i["duration_s"] > 180 for i in items) and all(
        s["duration_s"] < 60 for s in uploads["shorts"]["items"]
    )
    assert uploads["videos"]["avg_views"] > 0 and uploads["videos"]["median_views"] > 0
    top = kit["top_videos"]
    assert [t["rank"] for t in top] == [1, 2, 3, 4, 5] and top[0]["thumbnail_url"]
    traffic = kit["traffic_sources"]
    assert traffic[0] == {
        "code": "SHORTS",
        "label": "Shorts feed",
        "views": traffic[0]["views"],
        "share_pct": traffic[0]["share_pct"],
    }
    assert len(traffic) == 8


async def test_unavailable_sections_degrade_gracefully(make_client: Callable[..., Client]) -> None:
    api = FakeYouTubeAPI(reach_enabled=False, engaged_views_supported=False, monetized=False)
    async with make_client(api) as client:
        kit = await call(client, "get_media_kit_data", include_revenue=True)
    assert kit["impressions_funnel"]["impressions"] is None
    assert "not enabled" in kit["unavailable"]["impressions"]
    assert "Partner Program" in kit["unavailable"]["revenue"]
    assert kit["performance"]["engaged_views"]["value"] is None
    assert kit["performance_by_content_type"]["shorts"]["engaged_views"]["value"] is None
    assert kit["performance_by_content_type"]["videos"]["impressions"]["value"] is None
    assert kit["performance"]["views"]["value"] > 0  # the rest still works


async def test_saves_json_snapshot(client: Client, tmp_path: Path) -> None:
    kit = await call(client, "get_media_kit_data", save_to="kit/media-kit-data.json")
    saved = Path(kit["saved_to"])
    assert saved == (tmp_path / "out" / "kit" / "media-kit-data.json").resolve()
    assert json.loads(saved.read_text())["schema_version"] == "1"


async def test_documented_sample_matches_contract(kit: dict[str, Any]) -> None:
    """docs/examples/media-kit-data.sample.json must have exactly the shape the code produces.

    If this fails after an intentional, backwards-compatible change, regenerate the sample:
    uv run python scripts/generate_sample_media_kit.py
    """
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    generated = dict(kit)
    generated["recent_uploads"] = sample["recent_uploads"]  # the sample uses 6 uploads per format
    assert shape(sample) == shape(generated)
