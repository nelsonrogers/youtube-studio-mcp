"""MCP tool definitions, grouped by area."""

from __future__ import annotations

from mcp.server import MCPServer

from youtube_studio_mcp.context import AppContext
from youtube_studio_mcp.tools import account, analytics, charts, content, impressions, media_kit, write


def register_all(mcp: MCPServer, ctx: AppContext) -> None:
    account.register(mcp, ctx)
    content.register(mcp, ctx)
    analytics.register(mcp, ctx)
    impressions.register(mcp, ctx)
    media_kit.register(mcp, ctx)
    charts.register(mcp, ctx)
    if ctx.settings.enable_write:
        write.register(mcp, ctx)
