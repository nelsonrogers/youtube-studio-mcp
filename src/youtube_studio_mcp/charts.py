"""Charts for media kits and reports: public API (types, formatting) without heavy imports.

Rendering lives in :mod:`youtube_studio_mcp._chart_render` and is imported on first use, so the
MCP server starts quickly (matplotlib takes ~1 s to import, and much longer the very first time
it builds its font cache).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

ChartType = Literal["line", "area", "bar", "horizontal_bar", "stacked_bar", "donut"]
ValueFormat = Literal["auto", "number", "compact", "percent", "duration", "currency"]
Theme = Literal["light", "dark"]
ShowValues = Literal["auto", "none", "all", "last"]
OutputFormat = Literal["png", "svg", "pdf"]

MAX_SERIES = 8
MAX_DONUT_SLICES = 6
MAX_LABEL_CHARS = 34

PALETTE = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
}
INK = {
    "light": {
        "surface": "#fcfcfb",
        "primary": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "deemphasis": "#d6d4cc",
    },
    "dark": {
        "surface": "#1a1a19",
        "primary": "#f0efec",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "deemphasis": "#3d3d3a",
    },
}
FONT_PREFERENCE = ("Inter", "SF Pro Text", "Helvetica Neue", "Helvetica", "Segoe UI", "Arial", "DejaVu Sans")

# Mark specs, in points (1 CSS px ~ 0.75 pt).
LINE_WIDTH = 1.6
BAR_MAX_THICKNESS = 18.0
BAR_RADIUS = 3.0
SURFACE_GAP = 1.5
MARKER_SIZE = 7.0


class ChartError(ValueError):
    """Invalid chart input (message is user-facing)."""


@dataclass
class SeriesData:
    name: str
    values: list[float | None]


@dataclass
class ChartSpec:
    chart_type: ChartType
    labels: list[str]
    series: list[SeriesData]
    title: str
    subtitle: str | None = None
    x_label: str | None = None
    y_label: str | None = None
    value_format: ValueFormat = "auto"
    currency_symbol: str = "$"
    colors: list[str] | None = None
    theme: Theme = "light"
    transparent: bool = False
    width: int = 1600
    height: int = 900
    show_values: ShowValues = "auto"
    highlight: str | None = None
    source: str | None = None
    font_family: str | None = None
    y_min: float | None = None
    y_max: float | None = None
    _warnings: list[str] = field(default_factory=list)


@dataclass
class ChartResult:
    files: list[Path]
    preview_png: bytes
    warnings: list[str]


# --------------------------------------------------------------------------- formatting


def _compact(value: float) -> str:
    magnitude = abs(value)
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= threshold:
            scaled = value / threshold
            text = f"{scaled:.1f}" if abs(scaled) < 100 else f"{scaled:.0f}"
            return text.removesuffix(".0") + suffix
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def _duration(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_value(value: float | None, fmt: ValueFormat, *, currency: str = "$", axis_max: float = 0) -> str:
    """Format a number for labels/ticks. ``percent`` expects 0-100 values, ``duration`` seconds."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if fmt == "percent":
        return f"{value:.0f}%" if abs(value) >= 10 or float(value).is_integer() else f"{value:.1f}%"
    if fmt == "duration":
        return _duration(value)
    if fmt == "currency":
        body = _compact(value) if max(abs(value), axis_max) >= 10_000 else f"{value:,.2f}".removesuffix(".00")
        return f"-{currency}{body.lstrip('-')}" if value < 0 else f"{currency}{body}"
    if fmt == "compact" or (fmt in {"auto", "number"} and max(abs(value), axis_max) >= 10_000):
        return _compact(value)
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.1f}" if abs(value) >= 10 else f"{value:,.2f}".rstrip("0").rstrip(".")


def _truncate(label: str, limit: int = MAX_LABEL_CHARS) -> str:
    return label if len(label) <= limit else label[: limit - 1].rstrip() + "…"


def _parse_temporal(labels: Sequence[str]) -> tuple[list[date], str] | None:
    """Recognise ISO day (YYYY-MM-DD), month (YYYY-MM) or year (YYYY) labels."""
    if not labels:
        return None
    kind = None
    parsed: list[date] = []
    for raw in labels:
        text = str(raw).strip()
        try:
            if len(text) == 10:
                parsed.append(date.fromisoformat(text))
                this_kind = "day"
            elif len(text) == 7 and text[4] == "-":
                parsed.append(date(int(text[:4]), int(text[5:]), 1))
                this_kind = "month"
            elif len(text) == 4 and text.isdigit():
                parsed.append(date(int(text), 1, 1))
                this_kind = "year"
            else:
                return None
        except ValueError:
            return None
        if kind not in (None, this_kind):
            return None
        kind = this_kind
    assert kind is not None
    return parsed, kind


def _temporal_tick_label(day: date, kind: str, *, show_year: bool) -> str:
    if kind == "year":
        return str(day.year)
    if kind == "month":
        return f"{day:%b} {day.year}" if show_year else f"{day:%b}"
    return f"{day:%b} {day.day}, {day.year}" if show_year else f"{day:%b} {day.day}"


def render_chart(spec: ChartSpec, outputs: Sequence[tuple[Path, OutputFormat]]) -> ChartResult:
    """Render ``spec`` to every ``(path, format)`` in ``outputs`` and return a PNG preview."""
    from youtube_studio_mcp._chart_render import render

    return render(spec, outputs)
