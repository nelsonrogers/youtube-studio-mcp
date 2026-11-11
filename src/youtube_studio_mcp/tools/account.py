"""Sign-in status and browser-based sign-in."""

from __future__ import annotations

from typing import Annotated, Any

from google.oauth2.credentials import Credentials
from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from youtube_studio_mcp.auth import (
    ANALYTICS_MONETARY_READONLY,
    AUTH_COMMAND,
    YOUTUBE_MANAGE,
    AuthError,
    LoopbackOAuthFlow,
    NotAuthenticatedError,
    TokenStore,
    describe_scope,
    load_client_config,
    requested_scopes,
)
from youtube_studio_mcp.context import READ_ONLY, AppContext, tool_errors

SIGN_IN_TIMEOUT_SECONDS = 600


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(title="Auth status", annotations=READ_ONLY)
    @tool_errors
    def get_auth_status() -> dict[str, Any]:
        """Check whether the server is signed in to YouTube, which channel it reads, and which
        permissions were granted. Call this first if any other tool reports a sign-in problem."""
        settings = ctx.settings
        status: dict[str, Any] = {
            "profile": settings.profile,
            "signed_in": False,
            "write_tools_enabled": settings.enable_write,
            "token_file": str(settings.token_file),
            "output_dir": str(settings.output_dir),
        }
        flow = ctx.auth_flow
        if flow is not None and (not flow.finished or flow.status != "succeeded"):
            status["sign_in_attempt"] = {"status": flow.status, "error": flow.error}
            if not flow.finished:
                status["sign_in_attempt"]["sign_in_url"] = flow.auth_url
        try:
            granted = ctx.api.granted_scopes()
            channel = ctx.channel(fresh=True)
        except NotAuthenticatedError:
            try:
                load_client_config(settings)
                status["next_step"] = (
                    "Call `authenticate` to open Google sign-in in the user's browser, or ask them to "
                    f"run `{AUTH_COMMAND}` in a terminal."
                )
            except AuthError as exc:
                status["next_step"] = str(exc)
            return status
        except AuthError as exc:
            status["error"] = str(exc)
            return status
        wanted = requested_scopes(revenue=True, write=settings.enable_write)
        snippet = channel.get("snippet") or {}
        handle = snippet.get("customUrl")
        status.update(
            {
                "signed_in": True,
                "channel": {
                    "id": channel.get("id"),
                    "title": snippet.get("title"),
                    "handle": handle,
                    "url": f"https://www.youtube.com/{handle}" if handle else None,
                },
                "granted_permissions": [describe_scope(scope) for scope in granted],
                "revenue_access": ANALYTICS_MONETARY_READONLY in granted,
                "write_access": YOUTUBE_MANAGE in granted,
                "missing_permissions": [describe_scope(s) for s in wanted if s not in granted],
            }
        )
        if settings.enable_write and YOUTUBE_MANAGE not in granted:
            status["next_step"] = f"Write tools need extra permission: run `{AUTH_COMMAND} --write`."
        return status

    @mcp.tool(
        title="Sign in to YouTube",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )
    @tool_errors
    def authenticate(
        include_revenue: Annotated[
            bool,
            Field(
                description="Also request read access to revenue reports (only useful for monetized channels)."
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Start Google sign-in in the user's browser and return the sign-in URL immediately.

        The user must approve access in the browser (choosing the account or Brand Account that
        owns the channel). Afterwards call `get_auth_status` to confirm. Only works when the MCP
        server runs on the user's own computer. Signing in again replaces the stored token."""
        settings = ctx.settings
        client_config = load_client_config(settings)
        if ctx.auth_flow is not None and not ctx.auth_flow.finished:
            ctx.auth_flow.cancel()
        store = TokenStore(settings.token_file)

        def on_success(credentials: Credentials, granted: list[str]) -> None:
            store.save(credentials, granted)
            ctx.on_signed_in()

        flow = LoopbackOAuthFlow(
            client_config,
            requested_scopes(revenue=include_revenue, write=settings.enable_write),
            on_success=on_success,
            timeout_seconds=SIGN_IN_TIMEOUT_SECONDS,
        )
        url = flow.start(open_browser=True)
        ctx.auth_flow = flow
        return {
            "status": "waiting_for_user",
            "sign_in_url": url,
            "expires_in_seconds": SIGN_IN_TIMEOUT_SECONDS,
            "instructions": (
                "A browser tab should have opened with Google's sign-in page. If it did not, give the "
                "user the sign_in_url. They should pick the account that owns the channel, approve all "
                "permissions (an 'unverified app' warning is expected for a personal Google Cloud "
                "project: Advanced > Go to app), then tell you when done. Then call get_auth_status."
            ),
        }
