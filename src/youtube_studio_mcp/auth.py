"""Google OAuth for a local, installed-app style client.

The flow here is written to be safe inside a stdio MCP server:

* it never writes to stdout (that stream carries JSON-RPC messages), and
* it does not block the caller: :class:`LoopbackOAuthFlow` serves the redirect on a
  background thread, so an MCP tool can hand the sign-in URL back to the agent at once.

Tokens are stored per *profile* (one Google account / channel each) in the config
directory with ``0600`` permissions.
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from youtube_studio_mcp.config import Settings

logger = logging.getLogger(__name__)

YOUTUBE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
ANALYTICS_READONLY = "https://www.googleapis.com/auth/yt-analytics.readonly"
ANALYTICS_MONETARY_READONLY = "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"
YOUTUBE_MANAGE = "https://www.googleapis.com/auth/youtube.force-ssl"

SETUP_DOC_URL = "https://github.com/nelsonrogers/youtube-studio-mcp/blob/main/docs/google-cloud-setup.md"
AUTH_COMMAND = "youtube-studio-mcp auth"


class AuthError(Exception):
    """An authentication problem with a message meant for the user."""


class NotAuthenticatedError(AuthError):
    """No stored token for the active profile."""


def requested_scopes(*, revenue: bool = True, write: bool = False) -> list[str]:
    """The OAuth scopes to request for the given feature set."""
    scopes = [YOUTUBE_READONLY, ANALYTICS_READONLY]
    if revenue:
        scopes.append(ANALYTICS_MONETARY_READONLY)
    if write:
        scopes.append(YOUTUBE_MANAGE)
    return scopes


def describe_scope(scope: str) -> str:
    return {
        YOUTUBE_READONLY: "read channel, videos, playlists and comments",
        ANALYTICS_READONLY: "read YouTube Analytics reports",
        ANALYTICS_MONETARY_READONLY: "read revenue / ad-performance reports",
        YOUTUBE_MANAGE: "edit video details and manage comments",
    }.get(scope, scope)


# --------------------------------------------------------------------------- client secret


def load_client_config(settings: Settings) -> dict[str, Any]:
    """Load the OAuth client ("Desktop app") configuration.

    Sources, in order: ``YOUTUBE_STUDIO_MCP_CLIENT_ID`` + ``..._CLIENT_SECRET`` env vars,
    ``YOUTUBE_STUDIO_MCP_CLIENT_SECRET_FILE``, then ``<config dir>/client_secret.json``.
    """
    if settings.client_id and settings.client_secret:
        return {
            "installed": {
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
    if settings.client_id or settings.client_secret:
        raise AuthError(
            "Set both YOUTUBE_STUDIO_MCP_CLIENT_ID and YOUTUBE_STUDIO_MCP_CLIENT_SECRET, or neither."
        )

    path = settings.client_secret_file or settings.default_client_secret_file
    if not path.is_file():
        raise AuthError(
            f"No Google OAuth client found at {path}.\n"
            "Create a 'Desktop app' OAuth client in Google Cloud, download its JSON, then run:\n"
            f"  {AUTH_COMMAND} --client-secret /path/to/client_secret.json\n"
            f"Step-by-step guide: {SETUP_DOC_URL}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthError(f"Could not read the OAuth client file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AuthError(f"{path} is not a Google OAuth client file.")
    if "installed" in data:
        return data
    if "web" in data:
        raise AuthError(
            f"{path} is a 'Web application' OAuth client. This server needs a 'Desktop app' client: "
            f"create one in Google Cloud > APIs & Services > Credentials. Guide: {SETUP_DOC_URL}"
        )
    raise AuthError(f"{path} is not a Google OAuth client file (expected an 'installed' section).")


def install_client_secret(source: Path, settings: Settings) -> Path:
    """Validate a downloaded client secret file and copy it into the config directory."""
    probe = Settings(config_dir=settings.config_dir, client_secret_file=source)
    config = load_client_config(probe)  # raises AuthError when invalid
    destination = settings.default_client_secret_file
    _write_private(destination, json.dumps(config, indent=2))
    return destination


# --------------------------------------------------------------------------- token storage


def _write_private(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` readable only by the current user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        Path(tmp).replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class TokenStore:
    """Reads and writes the OAuth token for one profile."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> tuple[Credentials, list[str]] | None:
        """Return ``(credentials, granted_scopes)`` or None when no token is stored."""
        if not self.path.is_file():
            return None
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
            credentials = Credentials.from_authorized_user_info(info)
        except (OSError, ValueError) as exc:
            raise AuthError(
                f"The stored token at {self.path} is unreadable ({exc}). "
                f"Run `{AUTH_COMMAND}` to sign in again."
            ) from exc
        granted = info.get("granted_scopes") or info.get("scopes") or []
        if isinstance(granted, str):
            granted = granted.split()
        return credentials, [str(scope) for scope in granted]

    def save(self, credentials: Credentials, granted_scopes: Iterable[str]) -> None:
        info = json.loads(credentials.to_json())
        info["granted_scopes"] = sorted(set(granted_scopes))
        _write_private(self.path, json.dumps(info, indent=2))

    def delete(self) -> bool:
        if self.path.is_file():
            self.path.unlink()
            return True
        return False


_refresh_lock = threading.Lock()


def load_credentials(settings: Settings) -> tuple[Credentials, list[str]]:
    """Load stored credentials for the active profile, refreshing the access token if needed."""
    store = TokenStore(settings.token_file)
    loaded = store.load()
    if loaded is None:
        raise NotAuthenticatedError(
            f"Not signed in to YouTube (profile '{settings.profile}'). Ask the user to run "
            f"`{AUTH_COMMAND}` in a terminal, or call the `authenticate` tool to start sign-in "
            "in their browser."
        )
    credentials, granted = loaded
    with _refresh_lock:
        if not credentials.valid:
            if not credentials.refresh_token:
                raise AuthError(f"The stored token has no refresh token. Run `{AUTH_COMMAND}` again.")
            try:
                credentials.refresh(Request())
            except RefreshError as exc:
                raise AuthError(refresh_failed_message(exc)) from exc
            except TransportError as exc:
                raise AuthError(f"Could not reach Google to refresh the sign-in token: {exc}") from exc
            store.save(credentials, granted)
    return credentials, granted


def refresh_failed_message(exc: Exception) -> str:
    return (
        f"Google rejected the stored sign-in ({exc}). The token was revoked or has expired. "
        f"Run `{AUTH_COMMAND}` to sign in again. If this keeps happening about every 7 days, "
        "your Google Cloud OAuth consent screen is still in 'Testing' mode: set its publishing "
        f"status to 'In production' (see {SETUP_DOC_URL}#avoid-weekly-sign-ins)."
    )


# --------------------------------------------------------------------------- loopback OAuth flow

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:system-ui,-apple-system,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;
color:#1f2328;line-height:1.5}}h1{{font-size:1.4rem}}</style></head>
<body><h1>{title}</h1><p>{body}</p></body></html>"""


class _CallbackServer(HTTPServer):
    flow_owner: LoopbackOAuthFlow


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self) -> None:
        params = parse_qs(urlparse(self.path).query)
        if "code" not in params and "error" not in params:
            self.send_error(404)
            return
        ok, title, body = self.server.flow_owner._handle_callback(params)
        payload = _PAGE.format(title=html.escape(title), body=html.escape(body)).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("oauth callback: " + format, *args)


class LoopbackOAuthFlow:
    """OAuth 2.0 authorization-code flow (with PKCE) using a loopback redirect.

    Usage::

        flow = LoopbackOAuthFlow(client_config, scopes, on_success=save)
        url = flow.start()          # opens the browser, returns immediately
        flow.wait(timeout=300)      # optional: block until finished
    """

    def __init__(
        self,
        client_config: dict[str, Any],
        scopes: list[str],
        *,
        on_success: Callable[[Credentials, list[str]], None],
        timeout_seconds: float = 300,
    ) -> None:
        self.scopes = scopes
        self._on_success = on_success
        self._timeout = timeout_seconds
        self._server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
        self._server.flow_owner = self
        self._server.timeout = 0.5
        port = self._server.server_address[1]
        self.redirect_uri = f"http://127.0.0.1:{port}/"
        self._flow = Flow.from_client_config(client_config, scopes=scopes, redirect_uri=self.redirect_uri)
        auth_url, self._state = self._flow.authorization_url(
            access_type="offline",  # we need a refresh token
            prompt="consent",  # Google only returns a refresh token on (re-)consent
        )
        self.auth_url: str = str(auth_url)
        self.status = "pending"  # pending | succeeded | failed | timed_out | cancelled
        self.error: str | None = None
        self.granted_scopes: list[str] = []
        self.started_at = time.time()
        self._finished = threading.Event()
        self._callback_handled = False
        self._thread: threading.Thread | None = None

    # -- public API ---------------------------------------------------------------------------

    def start(self, *, open_browser: bool = True) -> str:
        self._thread = threading.Thread(target=self._serve, name="youtube-studio-mcp-oauth", daemon=True)
        self._thread.start()
        if open_browser:
            try:
                webbrowser.open(self.auth_url, new=1, autoraise=True)
            except Exception:
                logger.info("Could not open a browser automatically", exc_info=True)
        return self.auth_url

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the flow finishes; returns True when sign-in succeeded."""
        self._finished.wait(timeout)
        return self.status == "succeeded"

    def cancel(self) -> None:
        if self.status == "pending":
            self.status = "cancelled"
        self._finished.set()

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    @property
    def missing_scopes(self) -> list[str]:
        return [scope for scope in self.scopes if scope not in self.granted_scopes]

    # -- internals ----------------------------------------------------------------------------

    def _serve(self) -> None:
        deadline = time.monotonic() + self._timeout
        try:
            while not self._callback_handled and not self._finished.is_set():
                if time.monotonic() >= deadline:
                    self.status = "timed_out"
                    self.error = f"Sign-in was not completed within {int(self._timeout)} seconds."
                    break
                self._server.handle_request()
        finally:
            self._server.server_close()
            self._finished.set()

    def _handle_callback(self, params: dict[str, list[str]]) -> tuple[bool, str, str]:
        state = (params.get("state") or [""])[0]
        if not secrets.compare_digest(state, self._state or ""):
            # Not our request (or a stale tab): ignore it and keep waiting for the real redirect.
            return False, "Sign-in failed", "This response does not match the current sign-in request."
        self._callback_handled = True
        if "error" in params:
            reason = params["error"][0]
            if reason == "access_denied":
                return self._fail("Sign-in cancelled", "Access was not granted. You can close this tab.")
            return self._fail("Sign-in failed", f"Google returned an error: {reason}.")
        try:
            self._flow.fetch_token(code=params["code"][0])
            credentials = self._flow.credentials
            token_scope = self._flow.oauth2session.token.get("scope", "")
            granted = token_scope.split() if isinstance(token_scope, str) else list(token_scope)
            self.granted_scopes = granted or list(self.scopes)
            self._on_success(credentials, self.granted_scopes)
        except Exception as exc:
            logger.warning("OAuth token exchange failed", exc_info=True)
            return self._fail("Sign-in failed", f"Could not complete sign-in: {exc}")
        self.status = "succeeded"
        body = "You're signed in to YouTube Studio MCP. You can close this tab and return to your assistant."
        if self.missing_scopes:
            body += " Note: some permissions were not granted, so a few tools will be unavailable."
        return True, "Signed in", body

    def _fail(self, title: str, message: str) -> tuple[bool, str, str]:
        self.status = "failed"
        self.error = message
        return False, title, message
