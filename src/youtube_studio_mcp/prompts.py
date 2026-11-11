"""Reusable prompt templates (shown as slash commands / prompt pickers in MCP clients)."""

from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field


def register(mcp: MCPServer) -> None:
    @mcp.prompt(title="Channel check-up")
    def channel_checkup(
        period: Annotated[
            str, Field(description="e.g. last_28_days, last_90_days, last_month")
        ] = "last_28_days",
    ) -> str:
        """Review how the channel performed in a period and suggest what to do next."""
        return (
            f"Give me a YouTube channel check-up for {period}.\n"
            "1. get_performance_summary for the period (compare with the previous period).\n"
            "2. get_top_videos (by views, and by subscribersGained) and get_traffic_sources.\n"
            "3. get_audience_breakdown for content_type and subscribed_status.\n"
            "Then write a short report: what went up or down and why (point to specific videos and "
            "sources), what's working, and 3 concrete, data-backed ideas for the next uploads. "
            "Quote exact numbers and the date range, and say when data is too thin to conclude."
        )

    @mcp.prompt(title="Refresh media kit")
    def media_kit_refresh(
        output_folder: Annotated[
            str, Field(description="Folder for the JSON snapshot and charts")
        ] = "media-kit",
        brand_colors: Annotated[str, Field(description="Comma-separated hex colours, optional")] = "",
    ) -> str:
        """Collect fresh media-kit numbers and render the standard charts."""
        colors = f" Use these brand colours in order: {brand_colors}." if brand_colors else ""
        return (
            "Update my media kit with fresh YouTube data.\n"
            f"1. Call get_media_kit_data with save_to='{output_folder}/media-kit-data.json'.\n"
            "2. With create_chart, render into the same folder (PNG, plus extra_formats=['svg']):\n"
            "   - monthly views (line) and total subscribers (line) from monthly_trend\n"
            "   - audience age (bar, value_format='percent', highlight the largest group)\n"
            "   - gender split (donut, percent) and top countries (horizontal_bar)\n"
            "   - Shorts vs videos share (donut) if both exist\n"
            f"   Add a source_note with the date range.{colors}\n"
            "3. Summarise the headline numbers (highlights) in a short list I can paste, and mention "
            "anything listed under `unavailable`."
        )

    @mcp.prompt(title="Video deep dive")
    def video_deep_dive(
        video_id: Annotated[str, Field(description="The video to analyse")],
    ) -> str:
        """Explain how one video performed and what to learn from it."""
        return (
            f"Do a deep dive on video {video_id}.\n"
            "Use get_videos_details, get_performance_summary(video_id=..., period='all_time'), "
            "get_audience_retention, get_traffic_sources(video_id=...) and, if it gets search "
            "traffic, get_traffic_sources(video_id=..., source_type='YT_SEARCH'). Compare it with the "
            "channel's typical video (get_top_videos / list_videos). Explain the hook (first 30 "
            "seconds), where viewers drop off and why that might be, which sources drove views, and "
            "give specific suggestions for title, thumbnail and structure of the next video."
        )

    @mcp.prompt(title="Comment digest")
    def comment_digest(
        video_id: Annotated[str, Field(description="Optional video ID; empty = whole channel")] = "",
    ) -> str:
        """Summarise what the audience is saying in the comments."""
        target = f"on video {video_id}" if video_id else "across the channel"
        return (
            f"Read the latest ~200 comments {target} with list_comments (order='time').\n"
            "Group them into themes (praise, questions, requests/content ideas, criticism, spam), with "
            "counts and 1-2 short representative quotes each. List unanswered questions worth replying "
            "to (with comment IDs) and the top 5 content ideas viewers are asking for."
        )
