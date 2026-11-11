"""Thin, thread-safe access to the YouTube Data API v3 and YouTube Analytics API v2.

MCP tool functions run on worker threads and ``httplib2`` (used by
``google-api-python-client``) is not thread-safe, so each thread builds its own
service objects. Credentials are shared and refreshed under a lock.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from youtube_studio_mcp.auth import (
    AUTH_COMMAND,
    SETUP_DOC_URL,
    AuthError,
    load_credentials,
    refresh_failed_message,
)
from youtube_studio_mcp.config import Settings

MAX_IDS_PER_REQUEST = 50


class YouTubeAPIError(Exception):
    """A YouTube API failure with a message meant for the user (and their agent)."""

    def __init__(self, message: str, *, status: int | None = None, reasons: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.status = status
        self.reasons = tuple(reasons)


class YouTubeAPI(Protocol):
    """The minimal surface the tools need; tests provide a fake implementation."""

    def data(self, resource: str, method: str, **params: Any) -> dict[str, Any]:
        """Call ``youtube.<resource>().<method>(**params)`` on the Data API v3."""
        ...

    def analytics(self, **params: Any) -> dict[str, Any]:
        """Call ``youtubeAnalytics.reports().query(**params)`` on the Analytics API v2."""
        ...

    def reporting(self, resource: str, method: str, **params: Any) -> dict[str, Any]:
        """Call the YouTube Reporting API v1; ``resource`` is e.g. ``jobs`` or ``jobs.reports``."""
        ...

    def download(self, url: str) -> bytes:
        """Authorized GET of a Reporting API ``downloadUrl``."""
        ...

    def granted_scopes(self) -> list[str]:
        """OAuth scopes granted to the stored token (raises AuthError when signed out)."""
        ...

    def reset(self) -> None:
        """Drop cached credentials/services, e.g. after signing in again."""
        ...


@dataclass
class _ThreadState:
    generation: int = -1
    credentials: Any = None
    data: Any = None
    analytics: Any = None
    reporting: Any = None
    granted: list[str] = field(default_factory=list)


class GoogleYouTubeClient:
    """Production implementation of :class:`YouTubeAPI` backed by google-api-python-client."""

    def __init__(self, settings: Settings, *, num_retries: int = 3) -> None:
        self._settings = settings
        self._num_retries = num_retries
        self._local = threading.local()
        self._generation = 0
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._generation += 1

    def _state(self) -> _ThreadState:
        state: _ThreadState | None = getattr(self._local, "state", None)
        if state is None:
            state = _ThreadState()
            self._local.state = state
        if state.generation != self._generation:
            credentials, granted = load_credentials(self._settings)
            state.credentials = credentials
            state.data = build("youtube", "v3", credentials=credentials, cache_discovery=False)
            state.analytics = build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False)
            state.reporting = None  # built on first use
            state.granted = granted
            state.generation = self._generation
        return state

    def granted_scopes(self) -> list[str]:
        return list(self._state().granted)

    def data(self, resource: str, method: str, **params: Any) -> dict[str, Any]:
        service = self._state().data
        request = getattr(getattr(service, resource)(), method)(**params)
        return self._execute(request, api="YouTube Data API")

    def analytics(self, **params: Any) -> dict[str, Any]:
        request = self._state().analytics.reports().query(**params)
        return self._execute(request, api="YouTube Analytics API")

    def reporting(self, resource: str, method: str, **params: Any) -> dict[str, Any]:
        state = self._state()
        if state.reporting is None:
            state.reporting = build(
                "youtubereporting", "v1", credentials=state.credentials, cache_discovery=False
            )
        target = state.reporting
        for part in resource.split("."):
            target = getattr(target, part)()
        request = getattr(target, method)(**params)
        return self._execute(request, api="YouTube Reporting API")

    def download(self, url: str) -> bytes:
        if not url.startswith("https://youtubereporting.googleapis.com/"):
            raise YouTubeAPIError(f"Refusing to download from an unexpected host: {url}")
        session = AuthorizedSession(self._state().credentials)
        try:
            response = session.get(url, timeout=60)
        except (RefreshError, TransportError, OSError) as exc:
            raise YouTubeAPIError(f"Could not download the report: {exc}") from exc
        if response.status_code != 200:
            raise YouTubeAPIError(
                f"Report download failed with HTTP {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )
        return bytes(response.content)

    def _execute(self, request: Any, *, api: str) -> dict[str, Any]:
        try:
            # num_retries retries 5xx, 429 and transient 403 rate-limit errors with backoff.
            result = request.execute(num_retries=self._num_retries)
        except HttpError as exc:
            raise error_from_http(exc, api=api) from exc
        except RefreshError as exc:
            raise AuthError(refresh_failed_message(exc)) from exc
        except (TransportError, OSError, TimeoutError) as exc:
            raise YouTubeAPIError(f"Network error while calling the {api}: {exc}") from exc
        return result if isinstance(result, dict) else {}


def _error_payload(exc: HttpError) -> dict[str, Any]:
    try:
        content = exc.content.decode("utf-8") if isinstance(exc.content, bytes) else str(exc.content)
        payload = json.loads(content)
    except (ValueError, AttributeError):
        return {}
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else {}


def error_from_http(exc: HttpError, *, api: str) -> YouTubeAPIError:
    """Translate an HttpError into an actionable, user-facing message."""
    status = int(getattr(exc.resp, "status", 0) or 0)
    error = _error_payload(exc)
    message = str(error.get("message") or getattr(exc, "reason", "") or exc)
    reasons: list[str] = [str(e.get("reason")) for e in error.get("errors", []) if e.get("reason")]
    activation_url = None
    for detail in error.get("details", []) or []:
        if not isinstance(detail, dict):
            continue
        if detail.get("reason"):
            reasons.append(str(detail["reason"]))
        metadata = detail.get("metadata") or {}
        activation_url = activation_url or metadata.get("activationUrl")
    reason_set = set(reasons)

    if reason_set & {"quotaExceeded", "dailyLimitExceeded", "RATE_LIMIT_EXCEEDED"} and status != 429:
        hint = (
            "The daily API quota of your Google Cloud project is used up (the YouTube Data API allows "
            "10,000 units/day by default; most read calls cost 1 unit). It resets at midnight Pacific Time."
        )
    elif (
        reason_set & {"accessNotConfigured", "SERVICE_DISABLED"} or "has not been used in project" in message
    ):
        hint = (
            f"The {api} is not enabled in your Google Cloud project. Enable both 'YouTube Data API v3' "
            "and 'YouTube Analytics API' in Google Cloud > APIs & Services > Library"
            + (f" (direct link: {activation_url})" if activation_url else "")
            + f". Guide: {SETUP_DOC_URL}"
        )
    elif reason_set & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
        hint = (
            "The stored sign-in is missing a permission this request needs. Run "
            f"`{AUTH_COMMAND}` again and tick every checkbox on Google's consent screen "
            f"(use `{AUTH_COMMAND} --write` for the editing tools)."
        )
    elif reason_set & {"youtubeSignupRequired", "channelNotFound"}:
        hint = (
            "This Google account has no YouTube channel. Sign in again and pick the account or "
            "Brand Account that owns the channel."
        )
    elif status == 401:
        hint = f"Google did not accept the sign-in. Run `{AUTH_COMMAND}` to sign in again."
    elif status == 403 and api == "YouTube Analytics API":
        hint = (
            "Forbidden. Revenue/ad metrics need a monetized channel (YouTube Partner Program) and the "
            "revenue permission granted at sign-in; other reports need the channel owner to sign in."
        )
    elif status == 400 and api == "YouTube Analytics API":
        hint = (
            "YouTube rejected the report query. Usually the combination of dimensions, metrics, "
            "filters or sort is not supported, or a date is out of range. Adjust and retry."
        )
    elif status == 404 or reason_set & {"videoNotFound", "notFound", "playlistNotFound", "commentNotFound"}:
        hint = "Not found. Check the ID and that it belongs to the signed-in channel."
    elif status == 429 or status >= 500:
        hint = "YouTube is temporarily unavailable or rate-limiting requests. Try again in a minute."
    else:
        hint = ""

    text = f"{api} error {status}: {message}"
    if hint:
        text += f"\n{hint}"
    return YouTubeAPIError(text, status=status, reasons=reasons)


# --------------------------------------------------------------------------- Data API helpers


def chunked(items: Sequence[str], size: int = MAX_IDS_PER_REQUEST) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_duration(value: str | None) -> int | None:
    """Convert an ISO-8601 duration such as ``PT1H2M3S`` into seconds."""
    if not value:
        return None
    match = _DURATION_RE.match(value)
    if not match:
        return None
    parts = {key: int(raw) if raw else 0 for key, raw in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_my_channel(api: YouTubeAPI) -> dict[str, Any]:
    """The authenticated user's channel resource (raises if the account has no channel)."""
    response = api.data(
        "channels",
        "list",
        part="snippet,statistics,contentDetails,brandingSettings,status,topicDetails",
        mine=True,
    )
    items = response.get("items") or []
    if not items:
        raise YouTubeAPIError(
            "The signed-in Google account has no YouTube channel. Run "
            f"`{AUTH_COMMAND}` and choose the account or Brand Account that owns the channel."
        )
    channel: dict[str, Any] = items[0]
    return channel


def list_playlist_video_ids(api: YouTubeAPI, playlist_id: str, limit: int) -> list[str]:
    """Video IDs of a playlist in playlist order (for the uploads playlist: newest first)."""
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < limit:
        params: dict[str, Any] = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": min(50, limit - len(ids)),
        }
        if page_token:
            params["pageToken"] = page_token
        response = api.data("playlistItems", "list", **params)
        for item in response.get("items") or []:
            video_id = (item.get("contentDetails") or {}).get("videoId")
            if video_id:
                ids.append(video_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return ids[:limit]


VIDEO_PARTS = "snippet,contentDetails,statistics,status,liveStreamingDetails"


def get_videos(api: YouTubeAPI, video_ids: Sequence[str], parts: str = VIDEO_PARTS) -> list[dict[str, Any]]:
    """Fetch video resources in batches of 50, preserving the requested order."""
    unique = list(dict.fromkeys(video_ids))
    found: dict[str, dict[str, Any]] = {}
    for batch in chunked(unique):
        response = api.data("videos", "list", part=parts, id=",".join(batch), maxResults=len(batch))
        for item in response.get("items") or []:
            found[item["id"]] = item
    return [found[video_id] for video_id in unique if video_id in found]


def summarize_video(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Data API video resource into the fields agents usually need."""
    snippet = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    details = item.get("contentDetails") or {}
    status = item.get("status") or {}
    live = item.get("liveStreamingDetails")
    views = to_int(stats.get("viewCount"))
    likes = to_int(stats.get("likeCount"))
    comments = to_int(stats.get("commentCount"))
    duration = parse_duration(details.get("duration"))
    engagement = None
    if views:
        engagement = round(((likes or 0) + (comments or 0)) / views * 100, 2)
    return {
        "video_id": item.get("id"),
        "title": snippet.get("title"),
        "published_at": snippet.get("publishedAt"),
        "url": f"https://www.youtube.com/watch?v={item.get('id')}",
        "duration_seconds": duration,
        "format": guess_format(duration, live, snippet),
        "privacy_status": status.get("privacyStatus"),
        "views": views,
        "likes": likes,
        "comments": comments,
        "engagement_rate_pct": engagement,
        "tags": snippet.get("tags") or [],
        "category_id": snippet.get("categoryId"),
        "default_language": snippet.get("defaultLanguage") or snippet.get("defaultAudioLanguage"),
        "made_for_kids": status.get("madeForKids"),
        "thumbnail_url": ((snippet.get("thumbnails") or {}).get("high") or {}).get("url"),
    }


SHORTS_MAX_SECONDS = 180


def guess_format(duration: int | None, live: dict[str, Any] | None, snippet: dict[str, Any]) -> str:
    """Best-effort ``short`` / ``video`` / ``live`` classification from public metadata.

    The Data API has no Shorts flag. Since October 2024 Shorts can be up to 3 minutes,
    so anything at most 180 seconds is reported as ``short_or_video`` unless it is
    clearly a Short (<= 60s). Use the analytics ``creatorContentType`` breakdown for
    authoritative numbers.
    """
    if live or snippet.get("liveBroadcastContent") in {"live", "upcoming"}:
        return "live"
    if duration is None:
        return "unknown"
    if duration <= 60:
        return "short"
    if duration <= SHORTS_MAX_SECONDS:
        return "short_or_video"
    return "video"
