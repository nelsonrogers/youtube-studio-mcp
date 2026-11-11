"""Command-line entry point: run the MCP server, sign in, check status, sign out, self-test."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from youtube_studio_mcp import __version__
from youtube_studio_mcp.auth import (
    AuthError,
    LoopbackOAuthFlow,
    TokenStore,
    describe_scope,
    install_client_secret,
    load_client_config,
    requested_scopes,
)
from youtube_studio_mcp.config import Settings, env

logger = logging.getLogger("youtube_studio_mcp")


def _configure_logging(verbose: bool = False) -> None:
    level = "DEBUG" if verbose else (env("LOG_LEVEL") or "WARNING").upper()
    logging.basicConfig(
        stream=sys.stderr,  # stdout is reserved for MCP JSON-RPC messages
        level=getattr(logging, level, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _settings(args: argparse.Namespace, *, enable_write: bool | None = None) -> Settings:
    return Settings.from_env(profile=getattr(args, "profile", None), enable_write=enable_write)


# --------------------------------------------------------------------------- commands


def cmd_serve(args: argparse.Namespace) -> int:
    from youtube_studio_mcp.server import build_server

    settings = _settings(args, enable_write=True if args.enable_write else None)
    build_server(settings).run("stdio")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    # Flush every line: when output is piped (not a terminal), the sign-in URL must appear at once.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    settings = _settings(args, enable_write=True if args.write else None)
    if args.client_secret:
        destination = install_client_secret(Path(args.client_secret).expanduser(), settings)
        print(f"Saved the OAuth client to {destination}")
    client_config = load_client_config(settings)
    scopes = requested_scopes(revenue=not args.no_revenue, write=args.write or settings.enable_write)
    store = TokenStore(settings.token_file)
    flow = LoopbackOAuthFlow(client_config, scopes, on_success=store.save, timeout_seconds=args.timeout)
    url = flow.start(open_browser=not args.no_browser)
    print("Sign in with the Google account (or Brand Account) that owns the YouTube channel.")
    print("If you manage several channels, Google asks which one: pick the channel you want here.")
    print("If your browser did not open, visit this URL:\n")
    print(f"  {url}\n")
    print("Waiting for you to finish in the browser (Ctrl+C to cancel)...")
    try:
        while not flow.finished:
            flow.wait(timeout=0.5)
    except KeyboardInterrupt:
        flow.cancel()
        print("\nCancelled.")
        return 130
    if flow.status != "succeeded":
        print(f"Sign-in did not complete: {flow.error or flow.status}", file=sys.stderr)
        return 1

    from youtube_studio_mcp.youtube import GoogleYouTubeClient, get_my_channel

    channel = get_my_channel(GoogleYouTubeClient(settings))
    snippet = channel.get("snippet") or {}
    print(f"\nSigned in. Channel: {snippet.get('title')} {snippet.get('customUrl') or ''}".rstrip())
    print(f"Profile '{settings.profile}', token saved to {settings.token_file}")
    if flow.missing_scopes:
        print("\nWarning: these permissions were not granted, so some tools won't work:")
        for scope in flow.missing_scopes:
            print(f"  - {describe_scope(scope)}")
        print("Run this command again and tick every checkbox to fix it.")
    if not args.no_impressions:
        _enable_impressions(settings)
    print("\nYou're all set. Add the server to your MCP client (see the README) and restart it.")
    return 0


def _enable_impressions(settings: Settings) -> None:
    """Create the Reporting API job behind impressions/CTR so data starts accumulating now."""
    from youtube_studio_mcp.context import AppContext
    from youtube_studio_mcp.tools.impressions import JOB_NAME, REACH_REPORT_TYPE, find_reach_job
    from youtube_studio_mcp.youtube import GoogleYouTubeClient, YouTubeAPIError

    ctx = AppContext(settings=settings, api=GoogleYouTubeClient(settings))
    try:
        if find_reach_job(ctx) is None:
            ctx.api.reporting("jobs", "create", body={"reportTypeId": REACH_REPORT_TYPE, "name": JOB_NAME})
            print("Enabled impressions & CTR reports (first data in ~48 hours, with 30 days of history).")
    except (AuthError, YouTubeAPIError) as exc:
        print(
            f"Note: could not enable impressions reports ({str(exc).splitlines()[0]}). "
            "You can retry later with the enable_impressions_reports tool."
        )


def cmd_media_kit(args: argparse.Namespace) -> int:
    from youtube_studio_mcp.context import AppContext
    from youtube_studio_mcp.tools.media_kit import build_media_kit
    from youtube_studio_mcp.youtube import GoogleYouTubeClient

    settings = _settings(args)
    ctx = AppContext(settings=settings, api=GoogleYouTubeClient(settings))
    date_range = ctx.resolve(args.period, args.start_date, args.end_date, default="last_28_days")
    snapshot = build_media_kit(
        ctx,
        date_range,
        recent_uploads_per_format=args.recent_uploads,
        include_revenue=args.include_revenue,
    )
    text = json.dumps(snapshot, indent=2, ensure_ascii=False)
    if args.out == "-":
        print(text)
        return 0
    path = ctx.output_path(args.out, default_stem="media-kit", allowed=(".json",), default_suffix=".json")
    path.write_text(text + "\n", encoding="utf-8")
    print(f"Saved media-kit data ({date_range.start} to {date_range.end}) to {path}")
    if snapshot["unavailable"]:
        print("Unavailable sections: " + ", ".join(sorted(snapshot["unavailable"])))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    info: dict[str, Any] = {
        "version": __version__,
        "profile": settings.profile,
        "config_dir": str(settings.config_dir),
        "output_dir": str(settings.output_dir),
        "token_file": str(settings.token_file),
        "write_tools_enabled": settings.enable_write,
    }
    try:
        load_client_config(settings)
        info["oauth_client"] = "configured"
    except AuthError as exc:
        info["oauth_client"] = f"missing - {exc}"
    loaded = TokenStore(settings.token_file).load() if settings.token_file.exists() else None
    if loaded is None:
        info["signed_in"] = False
    else:
        from youtube_studio_mcp.youtube import GoogleYouTubeClient, YouTubeAPIError, get_my_channel

        info["granted_permissions"] = [describe_scope(scope) for scope in loaded[1]]
        try:
            channel = get_my_channel(GoogleYouTubeClient(settings))
            snippet = channel.get("snippet") or {}
            info["signed_in"] = True
            info["channel"] = f"{snippet.get('title')} ({snippet.get('customUrl') or channel.get('id')})"
        except (AuthError, YouTubeAPIError) as exc:
            info["signed_in"] = False
            info["error"] = str(exc)
    print(json.dumps(info, indent=2))
    return 0 if info.get("signed_in") else 1


def cmd_logout(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = TokenStore(settings.token_file)
    loaded = store.load() if store.exists() else None
    if loaded is None:
        print(f"Not signed in (no token at {settings.token_file}).")
        return 0
    credentials, _ = loaded
    if not args.keep_access:
        import requests

        token = credentials.refresh_token or credentials.token
        try:
            response = requests.post(
                "https://oauth2.googleapis.com/revoke",
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            print("Revoked access with Google." if response.ok else "Google did not confirm revocation.")
        except requests.RequestException as exc:
            print(f"Could not reach Google to revoke access ({exc}); deleting the local token anyway.")
    store.delete()
    print(f"Signed out of profile '{settings.profile}'.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    import anyio

    from youtube_studio_mcp.selfcheck import run_checks

    return anyio.run(run_checks, _settings(args))


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtube-studio-mcp",
        description="MCP server for YouTube Studio data. Run without a command to start the server (stdio).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging to stderr.")
    profile = argparse.ArgumentParser(add_help=False)
    profile.add_argument(
        "--profile",
        help="Named account/channel profile (default: 'default', or YOUTUBE_STUDIO_MCP_PROFILE).",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    serve = sub.add_parser("serve", parents=[profile], help="Run the MCP server over stdio (default).")
    serve.add_argument("--enable-write", action="store_true", help="Expose tools that edit videos/comments.")
    serve.set_defaults(func=cmd_serve)

    auth = sub.add_parser("auth", parents=[profile], help="Sign in to YouTube in your browser.")
    auth.add_argument("--client-secret", metavar="PATH", help="Install a downloaded OAuth client JSON first.")
    auth.add_argument(
        "--comments",
        "--write",
        dest="write",
        action="store_true",
        help="Also grant YouTube's 'manage' permission: needed to read comments and for the editing tools.",
    )
    auth.add_argument("--no-revenue", action="store_true", help="Don't request access to revenue reports.")
    auth.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser.")
    auth.add_argument("--timeout", type=int, default=300, help="Seconds to wait for sign-in (default 300).")
    auth.add_argument(
        "--no-impressions", action="store_true", help="Don't enable the impressions/CTR reporting job."
    )
    auth.set_defaults(func=cmd_auth)

    kit = sub.add_parser(
        "media-kit", parents=[profile], help="Write the media-kit JSON snapshot without an AI client."
    )
    kit.add_argument("--out", default="media-kit-data.json", help="Output .json path, or '-' for stdout.")
    kit.add_argument("--period", default="last_28_days", help="Named period (default last_28_days).")
    kit.add_argument("--start-date", help="Custom start YYYY-MM-DD (overrides --period).")
    kit.add_argument("--end-date", help="Custom end YYYY-MM-DD.")
    kit.add_argument("--recent-uploads", type=int, default=10, help="Recent uploads per format (default 10).")
    kit.add_argument("--include-revenue", action="store_true", help="Include revenue metrics.")
    kit.set_defaults(func=cmd_media_kit)

    status = sub.add_parser("status", parents=[profile], help="Show sign-in status and configuration.")
    status.set_defaults(func=cmd_status)

    logout = sub.add_parser("logout", parents=[profile], help="Revoke access and delete the stored token.")
    logout.add_argument("--keep-access", action="store_true", help="Only delete the local token.")
    logout.set_defaults(func=cmd_logout)

    check = sub.add_parser("check", parents=[profile], help="Call every read-only tool against your channel.")
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    if not getattr(args, "command", None):
        args = parser.parse_args(["serve", *(argv if argv is not None else sys.argv[1:])])
    from youtube_studio_mcp.youtube import YouTubeAPIError

    try:
        return int(args.func(args))
    except (AuthError, YouTubeAPIError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
