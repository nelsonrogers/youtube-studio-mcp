"""MCP server factory."""

from __future__ import annotations

from mcp.server import MCPServer

from youtube_studio_mcp import __version__, prompts
from youtube_studio_mcp.config import Settings
from youtube_studio_mcp.context import AppContext
from youtube_studio_mcp.tools import register_all
from youtube_studio_mcp.youtube import GoogleYouTubeClient, YouTubeAPI

INSTRUCTIONS = """\
Tools for a YouTube creator's own channel (YouTube Studio data via the official APIs).

Getting started: if a tool reports that you are not signed in, call get_auth_status, then
`authenticate` (opens Google sign-in in the user's browser) and wait for the user to finish.

Picking tools:
- Overview / "how am I doing": get_performance_summary (period vs previous period).
- Trends and growth charts: get_metrics_over_time (granularity day/week/month/year).
- Best/worst content: get_top_videos; catalogue and lifetime stats: list_videos.
- Audience (age, gender, countries, devices, Shorts vs long-form, subscribers vs not):
  get_audience_breakdown. Discovery: get_traffic_sources (source_type='YT_SEARCH' = search terms).
- One video: get_videos_details, get_audience_retention, and video_id on the analytics tools.
- Media kit / sponsor deck: get_media_kit_data, then create_chart for visuals.
- Money: get_revenue (monetized channels). Impressions & CTR: get_impressions (beta).
- Anything else: query_analytics (raw YouTube Analytics API v2).

Data notes: analytics dates are US Pacific time and lag ~2-3 days; periods end at the latest
complete day and every result states its exact date range — quote it. Percentages are 0-100.
Public subscriber counts are rounded by YouTube. Don't invent numbers that a tool didn't return.
"""


def build_server(settings: Settings | None = None, api: YouTubeAPI | None = None) -> MCPServer:
    """Create the MCP server. ``api`` can be injected for tests."""
    settings = settings or Settings.from_env()
    ctx = AppContext(settings=settings, api=api or GoogleYouTubeClient(settings))
    mcp = MCPServer(
        name="youtube-studio-mcp",
        title="YouTube Studio",
        description="YouTube Studio analytics, audience insights, media-kit data and charts for creators.",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/nelsonrogers/youtube-studio-mcp",
        version=__version__,
    )
    register_all(mcp, ctx)
    prompts.register(mcp)
    return mcp
