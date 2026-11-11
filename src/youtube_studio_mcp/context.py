"""State shared by all tools, plus helpers for arguments, errors and output files."""

from __future__ import annotations

import csv
import functools
import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from youtube_studio_mcp.auth import AuthError, LoopbackOAuthFlow
from youtube_studio_mcp.charts import ChartError
from youtube_studio_mcp.config import Settings
from youtube_studio_mcp.periods import DateRange, Period, PeriodError, resolve_period
from youtube_studio_mcp.youtube import YouTubeAPI, YouTubeAPIError, get_my_channel

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

CHANNEL_CACHE_SECONDS = 300

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
LOCAL_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

# --------------------------------------------------------------------------- shared argument types

PeriodArg = Annotated[
    Period | None,
    Field(
        description=(
            "Named date range, Studio-style. Rolling ranges end at the latest complete day of data "
            "(analytics lag ~2-3 days; dates are US Pacific). Ignored when start_date is set."
        )
    ),
]
StartDateArg = Annotated[str | None, Field(description="Custom range start, YYYY-MM-DD. Overrides `period`.")]
EndDateArg = Annotated[
    str | None,
    Field(description="Custom range end, YYYY-MM-DD (inclusive). Defaults to the latest complete day."),
]
VideoIdArg = Annotated[
    str | None,
    Field(
        description="Limit to one video (11-character ID, e.g. 'dQw4w9WgXcQ'). Omit for the whole channel."
    ),
]
SaveToArg = Annotated[
    str | None,
    Field(
        description=(
            "Optional file to also save the rows to (.csv or .json). A bare filename goes in the output "
            "directory; absolute paths are allowed."
        )
    ),
]


@dataclass
class AppContext:
    settings: Settings
    api: YouTubeAPI
    auth_flow: LoopbackOAuthFlow | None = None
    _channel: tuple[float, dict[str, Any]] | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- channel ---------------------------------------------------------------------------

    def channel(self, *, fresh: bool = False) -> dict[str, Any]:
        """The authenticated channel resource (cached for a few minutes)."""
        with self._lock:
            cached = self._channel
        if cached and not fresh and time.monotonic() - cached[0] < CHANNEL_CACHE_SECONDS:
            return cached[1]
        channel = get_my_channel(self.api)
        with self._lock:
            self._channel = (time.monotonic(), channel)
        return channel

    def channel_id(self) -> str:
        return str(self.channel()["id"])

    def uploads_playlist_id(self) -> str:
        related = (self.channel().get("contentDetails") or {}).get("relatedPlaylists") or {}
        playlist = related.get("uploads")
        if not playlist:
            raise YouTubeAPIError("Could not find the channel's uploads playlist.")
        return str(playlist)

    def channel_start(self) -> date:
        published = (self.channel().get("snippet") or {}).get("publishedAt")
        if not published:
            return date(2005, 2, 14)
        return datetime.fromisoformat(str(published).replace("Z", "+00:00")).date()

    def clear_cache(self) -> None:
        with self._lock:
            self._channel = None

    def on_signed_in(self) -> None:
        self.api.reset()
        self.clear_cache()

    # -- arguments -------------------------------------------------------------------------

    def resolve(
        self,
        period: str | None,
        start_date: str | None,
        end_date: str | None,
        *,
        default: str = "last_28_days",
    ) -> DateRange:
        return resolve_period(period, start_date, end_date, channel_start=self.channel_start, default=default)

    # -- files -----------------------------------------------------------------------------

    def output_path(
        self, filename: str | None, *, default_stem: str, allowed: Sequence[str], default_suffix: str
    ) -> Path:
        """Resolve where to write an output file, creating parent directories."""
        if filename:
            path = Path(filename).expanduser()
            if not path.is_absolute():
                path = self.settings.output_dir / path
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.settings.output_dir / f"{default_stem}-{stamp}{default_suffix}"
        if not path.suffix:
            path = path.with_suffix(default_suffix)
        if path.suffix.lower() not in allowed:
            raise ToolError(f"Unsupported file type {path.suffix!r}; use one of: {', '.join(allowed)}.")
        resolved = path.resolve()
        config_dir = self.settings.config_dir.resolve()
        if resolved == config_dir or config_dir in resolved.parents:
            raise ToolError("Refusing to write inside the credentials directory; choose another location.")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def save_rows(self, rows: Sequence[dict[str, Any]], filename: str, *, default_stem: str) -> str:
        path = self.output_path(
            filename, default_stem=default_stem, allowed=(".csv", ".json"), default_suffix=".csv"
        )
        if path.suffix.lower() == ".json":
            path.write_text(json.dumps(list(rows), indent=2, default=str), encoding="utf-8")
        else:
            columns: list[str] = []
            for row in rows:
                for key in row:
                    if key not in columns:
                        columns.append(key)
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow(
                        {k: (json.dumps(v) if isinstance(v, list | dict) else v) for k, v in row.items()}
                    )
        return str(path)

    def save_json(self, payload: Any, filename: str, *, default_stem: str) -> str:
        path = self.output_path(
            filename, default_stem=default_stem, allowed=(".json",), default_suffix=".json"
        )
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return str(path)


def tool_errors(fn: Callable[P, R]) -> Callable[P, R]:
    """Turn anticipated failures into ``ToolError`` so the agent sees a useful message.

    Unexpected exceptions are re-raised; the SDK logs them and reports a generic error.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (AuthError, YouTubeAPIError, PeriodError, ChartError) as exc:
            raise ToolError(str(exc)) from exc
        except ValueError as exc:
            raise ToolError(f"Invalid input: {exc}") from exc

    return wrapper


def require_scope(ctx: AppContext, scope: str, *, purpose: str, command: str) -> None:
    granted = ctx.api.granted_scopes()
    if granted and scope not in granted:
        raise ToolError(
            f"{purpose} needs a permission that was not granted at sign-in. Ask the user to run "
            f"`{command}` and approve all requested permissions."
        )
