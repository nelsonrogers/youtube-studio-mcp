"""End-to-end tests through the MCP client against the fake YouTube API."""

from __future__ import annotations

import base64
import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from tests.fakes import FakeYouTubeAPI
from youtube_studio_mcp.auth import ANALYTICS_READONLY, YOUTUBE_READONLY

pytestmark = pytest.mark.anyio

READ_TOOLS = {
    "get_auth_status",
    "authenticate",
    "get_channel",
    "list_videos",
    "get_videos_details",
    "list_playlists",
    "list_comments",
    "get_performance_summary",
    "get_metrics_over_time",
    "get_top_videos",
    "get_audience_breakdown",
    "get_traffic_sources",
    "get_audience_retention",
    "get_revenue",
    "query_analytics",
    "enable_impressions_reports",
    "get_impressions",
    "get_media_kit_data",
    "create_chart",
}
WRITE_TOOLS = {"update_video_details", "reply_to_comment", "moderate_comments"}


async def call(client: Client, name: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    text = next((block.text for block in result.content if getattr(block, "text", None)), "")
    assert not result.is_error, f"{name} failed: {text}"
    payload = result.structured_content
    return payload if isinstance(payload, dict) else json.loads(text)


async def call_error(client: Client, name: str, **arguments: Any) -> str:
    result = await client.call_tool(name, arguments)
    assert result.is_error, f"{name} unexpectedly succeeded"
    return "\n".join(block.text for block in result.content if getattr(block, "text", None))


async def test_tool_list_and_annotations(client: Client) -> None:
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == READ_TOOLS
    assert tools["get_channel"].annotations.read_only_hint is True
    assert tools["authenticate"].annotations.read_only_hint is False
    for tool in tools.values():
        assert tool.description and len(tool.description) > 40, tool.name
    prompts = {p.name for p in (await client.list_prompts()).prompts}
    assert prompts == {"channel_checkup", "media_kit_refresh", "video_deep_dive", "comment_digest"}


async def test_write_tools_are_opt_in(make_client: Callable[..., Client]) -> None:
    async with make_client(enable_write=True) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names >= WRITE_TOOLS


async def test_signed_out_errors_point_to_sign_in(make_client: Callable[..., Client]) -> None:
    async with make_client(FakeYouTubeAPI(signed_in=False)) as client:
        message = await call_error(client, "get_channel")
        assert "authenticate" in message and "youtube-studio-mcp auth" in message
        status = await call(client, "get_auth_status")
        assert status["signed_in"] is False
        assert "Desktop app" in status["next_step"]  # no OAuth client configured yet


async def test_auth_status(client: Client) -> None:
    status = await call(client, "get_auth_status")
    assert status["signed_in"] is True
    assert status["channel"]["handle"] == "@mapleandthread"
    assert status["revenue_access"] is True and status["write_access"] is False


async def test_channel_and_videos(client: Client) -> None:
    channel = await call(client, "get_channel")
    assert channel["subscribers"] == 48400 and channel["topics"] == ["Hobby", "Lifestyle (sociology)"]
    videos = await call(client, "list_videos", limit=5)
    assert videos["count"] == 5 and "tags" not in videos["videos"][0]
    shorts = await call(client, "list_videos", limit=50, content_format="short", sort_by="most_viewed")
    assert shorts["videos"] and all(v["format"] in {"short", "short_or_video"} for v in shorts["videos"])
    views = [v["views"] for v in shorts["videos"]]
    assert views == sorted(views, reverse=True)
    details = await call(client, "get_videos_details", video_ids=["vid00000000", "missing"])
    assert details["not_found"] == ["missing"] and details["videos"][0]["has_captions"] is True


async def test_list_videos_can_save_csv(client: Client, settings_out: Path) -> None:
    result = await call(client, "list_videos", limit=3, save_to="videos.csv")
    saved = Path(result["saved_to"])
    assert saved.parent == settings_out and saved.read_text().startswith("video_id,")


@pytest.fixture
def settings_out(tmp_path: Path) -> Path:
    return (tmp_path / "out").resolve()


MANAGE = "https://www.googleapis.com/auth/youtube.force-ssl"


async def test_playlists_and_comments(make_client: Callable[..., Client]) -> None:
    api = FakeYouTubeAPI(granted=[YOUTUBE_READONLY, ANALYTICS_READONLY, MANAGE])
    async with make_client(api) as client:
        playlists = await call(client, "list_playlists")
        assert playlists["count"] == 3
        comments = await call(client, "list_comments", limit=5, include_replies=True)
        assert comments["count"] == 5
        assert comments["comments"][0]["replies"][0]["author_is_channel_owner"] is True
        status = await call(client, "get_auth_status")
        assert status["comments_access"] is True


async def test_comments_explain_the_manage_permission(client: Client) -> None:
    # Verified against the live API: commentThreads.list rejects read-only tokens.
    message = await call_error(client, "list_comments", limit=5)
    assert "no read-only permission" in message and "auth --comments" in message
    assert (await call(client, "get_auth_status"))["comments_access"] is False


async def test_performance_summary(client: Client, fake_api: FakeYouTubeAPI) -> None:
    summary = await call(client, "get_performance_summary", period="last_28_days", include_revenue=True)
    assert summary["date_range"] == {
        "start_date": "2026-10-12",
        "end_date": "2026-11-08",
        "days": 28,
        "label": "last_28_days",
    }
    metrics = summary["metrics"]
    for name in ("views", "engagedViews", "watchTimeHours", "netSubscribers", "engagementRatePct", "rpm"):
        assert metrics[name]["value"] is not None, name
        assert "change_pct" in metrics[name]
    assert (
        metrics["netSubscribers"]["value"]
        == metrics["subscribersGained"]["value"] - metrics["subscribersLost"]["value"]
    )


async def test_summary_without_engaged_views_or_monetization(make_client: Callable[..., Client]) -> None:
    api = FakeYouTubeAPI(engaged_views_supported=False, monetized=False)
    async with make_client(api) as client:
        summary = await call(client, "get_performance_summary", include_revenue=True)
    assert "engagedViews" not in summary["metrics"]
    assert "YouTube Partner Program" in summary["revenue_unavailable"]


async def test_metrics_over_time_monthly_with_cumulative_subscribers(client: Client) -> None:
    result = await call(
        client,
        "get_metrics_over_time",
        granularity="month",
        period="last_365_days",
        include_cumulative_subscribers=True,
    )
    rows = result["rows"]
    assert 12 <= len(rows) <= 13
    assert rows[0]["partial"] is True and rows[-1]["partial"] is True
    for previous, row in itertools.pairwise(rows):
        assert row["totalSubscribers"] == previous["totalSubscribers"] + row["netSubscribers"]


async def test_metrics_over_time_content_type_filter(client: Client) -> None:
    all_rows = (await call(client, "get_metrics_over_time", metrics=["views"], period="last_7_days"))["rows"]
    shorts = (
        await call(
            client, "get_metrics_over_time", metrics=["views"], period="last_7_days", content_type="shorts"
        )
    )["rows"]
    assert len(all_rows) == len(shorts) == 7
    assert all(s["views"] < a["views"] for s, a in zip(shorts, all_rows, strict=True))
    message = await call_error(
        client, "get_metrics_over_time", include_cumulative_subscribers=True, content_type="shorts"
    )
    assert "whole channel" in message


async def test_top_videos(client: Client) -> None:
    result = await call(client, "get_top_videos", limit=5)
    videos = result["videos"]
    assert [v["rank"] for v in videos] == [1, 2, 3, 4, 5]
    assert videos[0]["title"] and videos[0]["content_type"] in {"Videos", "Shorts", "Live"}
    shorts = await call(client, "get_top_videos", limit=3, content_type="shorts")
    assert all(v["content_type"] == "Shorts" for v in shorts["videos"])


@pytest.mark.parametrize(
    "dimension",
    [
        "age",
        "gender",
        "age_gender",
        "country",
        "us_state",
        "device",
        "operating_system",
        "subscribed_status",
        "content_type",
        "playback_location",
        "sharing_service",
    ],
)
async def test_audience_breakdowns(client: Client, dimension: str) -> None:
    result = await call(client, "get_audience_breakdown", dimension=dimension)
    assert result["rows"], dimension
    if dimension == "age_gender":
        assert sum(result["by_gender"].values()) == pytest.approx(100, abs=0.5)
    if dimension == "country":
        assert result["rows"][0]["country"] == "US"
        assert sum(r["share_pct"] for r in result["rows"]) == pytest.approx(100, abs=0.1)


async def test_traffic_sources(client: Client) -> None:
    overview = await call(client, "get_traffic_sources")
    assert overview["rows"][0]["label"] == "Shorts feed"
    terms = await call(client, "get_traffic_sources", source_type="yt_search", limit=10)
    assert terms["source_type"] == "YT_SEARCH" and terms["rows"][0]["insightTrafficSourceDetail"]


async def test_audience_retention(client: Client) -> None:
    result = await call(client, "get_audience_retention", video_id="vid00000002", points=20)
    assert len(result["curve"]) <= 21 and result["curve"][-1]["elapsed_pct"] == 100.0
    summary = result["summary"]
    assert summary["retention_at_25pct"] > summary["retention_at_75pct"]
    assert summary["steepest_drops"]
    assert result["date_range"]["start_date"] == "2026-11-01"  # defaults to the video's lifetime


async def test_retention_for_brand_new_video(make_client: Callable[..., Client]) -> None:
    api = FakeYouTubeAPI()
    api.videos[0]["snippet"]["publishedAt"] = "2026-11-10T10:00:00Z"
    async with make_client(api) as client:
        message = await call_error(client, "get_audience_retention", video_id="vid00000000")
    assert "too recently" in message or "last ~3 days" in message


async def test_revenue(client: Client) -> None:
    result = await call(client, "get_revenue", period="last_90_days", top_videos_limit=3, currency="eur")
    assert result["currency"] == "EUR"
    assert result["totals"]["rpm"] > 0 and len(result["top_earning_videos"]) == 3
    assert all("rpm" in row for row in result["trend"])


async def test_revenue_requires_scope_and_monetization(make_client: Callable[..., Client]) -> None:
    async with make_client(FakeYouTubeAPI(granted=[YOUTUBE_READONLY, ANALYTICS_READONLY])) as client:
        assert "permission" in await call_error(client, "get_revenue")
    async with make_client(FakeYouTubeAPI(monetized=False)) as client:
        assert "Partner Program" in await call_error(client, "get_revenue")


async def test_query_analytics_aligns_month_dimension(client: Client) -> None:
    result = await call(
        client, "query_analytics", metrics=["views"], dimensions=["month"], period="last_90_days"
    )
    assert result["query"]["startDate"].endswith("-01") and result["query"]["endDate"].endswith("-01")
    assert result["notes"]


async def test_query_analytics_surfaces_api_errors(client: Client) -> None:
    message = await call_error(client, "query_analytics", metrics=["views"], dimensions=["video"])
    assert "400" in message and "sort" in message


async def test_impressions(client: Client) -> None:
    total = await call(client, "get_impressions", group_by="total", period="last_28_days")
    row = total["rows"][0]
    assert row["impressions"] > 0 and 3 <= row["ctr_pct"] <= 9
    assert "percent_0_100" in " ".join(total["notes"])
    by_video = await call(client, "get_impressions", group_by="video", limit=3)
    assert len(by_video["rows"]) == 3 and by_video["rows"][0]["title"]
    by_day = await call(client, "get_impressions", group_by="day", period="last_7_days")
    assert [r["date"] for r in by_day["rows"]] == sorted(r["date"] for r in by_day["rows"])


async def test_impressions_setup(make_client: Callable[..., Client]) -> None:
    api = FakeYouTubeAPI(reach_enabled=False)
    async with make_client(api) as client:
        assert "enable_impressions_reports" in await call_error(client, "get_impressions")
        created = await call(client, "enable_impressions_reports")
        assert created["status"] == "enabled"
        again = await call(client, "enable_impressions_reports")
        assert again["status"] == "already_enabled"
    assert len(api.created_jobs) == 1


async def test_create_chart_writes_files_and_preview(client: Client, settings_out: Path) -> None:
    result = await client.call_tool(
        "create_chart",
        {
            "chart_type": "bar",
            "labels": ["18-24", "25-34"],
            "series": [{"name": "Viewers", "values": [40, 60]}],
            "title": "Audience age",
            "value_format": "percent",
            "filename": "age",
            "extra_formats": ["svg"],
        },
    )
    assert not result.is_error
    files = [Path(f) for f in result.structured_content["files"]]
    assert [f.name for f in files] == ["age.png", "age.svg"] and all(f.parent == settings_out for f in files)
    image = next(block for block in result.content if block.type == "image")
    assert base64.b64decode(image.data).startswith(b"\x89PNG")


async def test_create_chart_rejects_bad_paths(client: Client, settings: Any) -> None:
    message = await call_error(
        client,
        "create_chart",
        chart_type="bar",
        labels=["a"],
        series=[{"name": "x", "values": [1]}],
        title="t",
        filename=str(settings.config_dir / "tokens" / "x.png"),
    )
    assert "credentials directory" in message
    message = await call_error(
        client,
        "create_chart",
        chart_type="bar",
        labels=["a"],
        series=[{"name": "x", "values": [1]}],
        title="t",
        filename="chart.exe",
    )
    assert "Unsupported file type" in message


async def test_write_tools(make_client: Callable[..., Client]) -> None:
    api = FakeYouTubeAPI(
        granted=[YOUTUBE_READONLY, ANALYTICS_READONLY, "https://www.googleapis.com/auth/youtube.force-ssl"]
    )
    async with make_client(api, enable_write=True) as client:
        preview = await call(client, "update_video_details", video_id="vid00000000", title="New title")
        assert preview["dry_run"] is True and preview["changes"]["title"]["after"] == "New title"
        assert not [c for c in api.calls if c[0] == "data.videos.update"]
        saved = await call(
            client,
            "update_video_details",
            video_id="vid00000000",
            title="New title",
            privacy_status="private",
            dry_run=False,
        )
        assert saved["saved"] is True
        _, params = next(c for c in api.calls if c[0] == "data.videos.update")
        assert params["part"] == "snippet,status"
        assert params["body"]["snippet"]["tags"] == ["tutorial", "studio"]  # untouched fields are preserved
        assert params["body"]["status"]["embeddable"] is True
        assert "100" in await call_error(
            client, "update_video_details", video_id="vid00000000", title="x" * 101
        )
        reply = await call(client, "reply_to_comment", comment_id="comment1", text="Thanks!")
        assert reply["reply_id"] == "newreply1"
        moderated = await call(
            client, "moderate_comments", comment_ids=["c1", "c2"], status="rejected", ban_author=True
        )
        assert moderated["updated"] == 2
        assert "rejected" in await call_error(
            client, "moderate_comments", comment_ids=["c1"], status="published", ban_author=True
        )


async def test_write_tools_need_write_scope(make_client: Callable[..., Client]) -> None:
    async with make_client(FakeYouTubeAPI(), enable_write=True) as client:
        message = await call_error(client, "reply_to_comment", comment_id="c", text="hi")
    assert "youtube-studio-mcp auth --write" in message
