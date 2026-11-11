"""`youtube-studio-mcp check`: exercise every read-only tool against the real channel.

Runs the server in-process through the MCP client, so it tests the same code path an
agent uses. Useful after setup, after upgrading, or when reporting a bug.
"""

from __future__ import annotations

import json
import time
from typing import Any

from mcp import Client

from youtube_studio_mcp.config import Settings
from youtube_studio_mcp.server import build_server

CHECKS: list[tuple[str, dict[str, Any]]] = [
    ("get_auth_status", {}),
    ("get_channel", {}),
    ("list_videos", {"limit": 5}),
    ("list_playlists", {"limit": 5}),
    ("list_comments", {"limit": 5}),
    ("get_performance_summary", {"period": "last_28_days"}),
    (
        "get_metrics_over_time",
        {"granularity": "month", "period": "last_365_days", "include_cumulative_subscribers": True},
    ),
    ("get_metrics_over_time", {"granularity": "week", "period": "last_90_days", "content_type": "shorts"}),
    ("get_top_videos", {"limit": 5}),
    ("get_top_videos", {"limit": 5, "content_type": "videos", "metric": "estimatedMinutesWatched"}),
    ("get_audience_breakdown", {"dimension": "age_gender"}),
    ("get_audience_breakdown", {"dimension": "country"}),
    ("get_audience_breakdown", {"dimension": "us_state"}),
    ("get_audience_breakdown", {"dimension": "device"}),
    ("get_audience_breakdown", {"dimension": "operating_system"}),
    ("get_audience_breakdown", {"dimension": "subscribed_status"}),
    ("get_audience_breakdown", {"dimension": "content_type"}),
    ("get_audience_breakdown", {"dimension": "playback_location"}),
    ("get_audience_breakdown", {"dimension": "sharing_service"}),
    ("get_traffic_sources", {}),
    ("get_traffic_sources", {"source_type": "YT_SEARCH"}),
    ("get_traffic_sources", {"source_type": "RELATED_VIDEO"}),
    ("query_analytics", {"metrics": ["views"], "dimensions": ["month"], "period": "last_365_days"}),
    ("get_media_kit_data", {}),
]


def _first_line(result: Any) -> str:
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return str(text).strip().splitlines()[0][:160]
    return ""


async def run_checks(settings: Settings) -> int:
    server = build_server(settings)
    failures = 0
    skipped = 0
    async with Client(server) as client:
        top_video: str | None = None
        checks = list(CHECKS)
        index = 0
        while index < len(checks):
            name, arguments = checks[index]
            index += 1
            started = time.monotonic()
            result = await client.call_tool(name, arguments)
            elapsed = time.monotonic() - started
            label = f"{name}({', '.join(f'{k}={v}' for k, v in arguments.items())})"
            full_text = " ".join(str(getattr(block, "text", "")) for block in result.content)
            if result.is_error and "auth --comments" in full_text:
                skipped += 1
                print(f"skip  {label}  (needs `youtube-studio-mcp auth --comments`)")
                continue
            if result.is_error:
                failures += 1
                print(f"FAIL  {label}  [{elapsed:.1f}s]\n      {_first_line(result)}")
                if name == "get_auth_status" or "Not signed in" in _first_line(result):
                    print("\nSign in first: youtube-studio-mcp auth")
                    return 1
                continue
            print(f"ok    {label}  [{elapsed:.1f}s]")
            if name == "get_top_videos" and top_video is None:
                payload = result.structured_content or json.loads(_first_line(result) or "{}")
                videos = (payload or {}).get("videos") or []
                if videos:
                    top_video = str(videos[0]["video_id"])
                    checks.append(("get_audience_retention", {"video_id": top_video}))
                    checks.append(("get_performance_summary", {"video_id": top_video, "period": "all_time"}))
                    checks.append(("get_videos_details", {"video_ids": [top_video]}))
    passed = len(checks) - failures - skipped
    print(f"\n{passed}/{len(checks)} checks passed" + (f", {skipped} skipped." if skipped else "."))
    if failures:
        print("Some reports may be unavailable for small channels or short periods; see messages above.")
    return 1 if failures else 0
