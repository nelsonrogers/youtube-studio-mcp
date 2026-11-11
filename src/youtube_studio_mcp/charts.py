"""Static chart rendering (PNG / SVG / PDF) with a clean, media-kit-ready style.

Uses matplotlib's object API only (no ``pyplot`` global state), so rendering is safe on
the worker threads MCP tool calls run on. The default palette is a colour-vision-
deficiency-checked categorical palette with matching light and dark variants.
"""

from __future__ import annotations

import io
import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import matplotlib

matplotlib.use("Agg")

from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import is_color_like
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch, PathPatch, Wedge
from matplotlib.path import Path as MplPath
from matplotlib.ticker import FuncFormatter, MaxNLocator

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


# --------------------------------------------------------------------------- validation


def _validate(spec: ChartSpec) -> None:
    if not spec.labels:
        raise ChartError("labels must contain at least one category or date.")
    if not spec.series:
        raise ChartError("series must contain at least one series.")
    for series in spec.series:
        if len(series.values) != len(spec.labels):
            raise ChartError(
                f"Series {series.name!r} has {len(series.values)} values but there are "
                f"{len(spec.labels)} labels; they must match."
            )
    if spec.chart_type == "donut":
        if len(spec.series) != 1:
            raise ChartError("A donut chart takes exactly one series (one value per slice).")
        if any(v is not None and v < 0 for v in spec.series[0].values):
            raise ChartError("Donut values cannot be negative.")
    elif len(spec.series) > MAX_SERIES:
        raise ChartError(
            f"At most {MAX_SERIES} series are supported; fold the smallest into an 'Other' series "
            "or split into several charts."
        )
    if spec.chart_type == "stacked_bar" and any(
        v is not None and v < 0 for series in spec.series for v in series.values
    ):
        raise ChartError("Stacked bars cannot contain negative values.")
    if not 400 <= spec.width <= 6000 or not 300 <= spec.height <= 6000:
        raise ChartError("width must be 400-6000 px and height 300-6000 px.")
    if spec.colors:
        for color in spec.colors:
            if not is_color_like(color):
                raise ChartError(f"{color!r} is not a valid colour (use hex like '#ff5a5f').")


# --------------------------------------------------------------------------- drawing helpers


@lru_cache(maxsize=16)
def _resolve_font(preferred: str | None) -> str:
    available = {entry.name for entry in font_manager.fontManager.ttflist}
    for name in ((preferred,) if preferred else ()) + FONT_PREFERENCE:
        if name in available:
            return name
    return "DejaVu Sans"


def _rounded_bar_path(
    x0: float, x1: float, base: float, end: float, rx: float, ry: float, *, horizontal: bool
) -> MplPath:
    """A bar that is square at its baseline and rounded at its data end."""
    k = 0.5523  # cubic Bezier approximation of a quarter circle
    if not horizontal:
        direction = 1.0 if end >= base else -1.0
        ry = min(ry, abs(end - base))
        rx = min(rx, (x1 - x0) / 2)
        top = end
        verts = [
            (x0, base),
            (x0, top - direction * ry),
            (x0, top - direction * ry * (1 - k)),
            (x0 + rx * (1 - k), top),
            (x0 + rx, top),
            (x1 - rx, top),
            (x1 - rx * (1 - k), top),
            (x1, top - direction * ry * (1 - k)),
            (x1, top - direction * ry),
            (x1, base),
            (x0, base),
        ]
    else:
        # x0/x1 are the band edges on the y axis; base/end run along x.
        direction = 1.0 if end >= base else -1.0
        rx = min(rx, abs(end - base))
        ry = min(ry, (x1 - x0) / 2)
        right = end
        verts = [
            (base, x0),
            (right - direction * rx, x0),
            (right - direction * rx * (1 - k), x0),
            (right, x0 + ry * (1 - k)),
            (right, x0 + ry),
            (right, x1 - ry),
            (right, x1 - ry * (1 - k)),
            (right - direction * rx * (1 - k), x1),
            (right - direction * rx, x1),
            (base, x1),
            (base, x0),
        ]
    codes = [
        MplPath.MOVETO,
        MplPath.LINETO,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.LINETO,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.LINETO,
        MplPath.CLOSEPOLY,
    ]
    return MplPath(verts, codes)


def _square_bar_path(x0: float, x1: float, y0: float, y1: float, *, horizontal: bool) -> MplPath:
    if horizontal:
        verts = [(y0, x0), (y1, x0), (y1, x1), (y0, x1), (y0, x0)]
    else:
        verts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return MplPath(verts, [MplPath.MOVETO, MplPath.LINETO, MplPath.LINETO, MplPath.LINETO, MplPath.CLOSEPOLY])


class _Renderer:
    """Builds one chart figure from a validated spec."""

    def __init__(self, spec: ChartSpec) -> None:
        self.spec = spec
        self.ink = INK[spec.theme]
        self.font = _resolve_font(spec.font_family)
        if spec.font_family and self.font != spec.font_family:
            spec._warnings.append(f"Font {spec.font_family!r} is not installed; used {self.font!r} instead.")
        base_colors = PALETTE[spec.theme]
        self.colors = list(spec.colors or []) + base_colors[len(spec.colors or []) :]
        self.dpi = 200
        self.fig = Figure(figsize=(spec.width / self.dpi, spec.height / self.dpi), dpi=self.dpi)
        self.canvas = FigureCanvasAgg(self.fig)
        surface = "none" if spec.transparent else self.ink["surface"]
        self.fig.patch.set_facecolor(surface)
        self.fig.patch.set_alpha(0 if spec.transparent else 1)
        scale = min(spec.width, spec.height * 16 / 9) / 1600
        self.fs_title = 13.5 * scale
        self.fs_sub = 9.5 * scale
        self.fs_tick = 8.0 * scale
        self.fs_label = 8.5 * scale
        self.fs_note = 7.5 * scale

        numbers = [v for s in spec.series for v in s.values if v is not None]
        if spec.chart_type == "stacked_bar":
            sums = [sum(s.values[i] or 0 for s in spec.series) for i in range(len(spec.labels))]
            numbers += sums
        self.axis_max = max((abs(v) for v in numbers), default=0.0)

    # -- text --------------------------------------------------------------------------------

    def text(self, x: float, y: float, value: str, *, size: float, color: str, **kwargs: Any) -> Any:
        return self.fig.text(x, y, value, fontsize=size, color=color, fontfamily=self.font, **kwargs)

    def fmt(self, value: float | None) -> str:
        return format_value(
            value, self.spec.value_format, currency=self.spec.currency_symbol, axis_max=self.axis_max
        )

    def pt_to_fig_h(self, points: float) -> float:
        return points / 72 / self.fig.get_figheight()

    def pt_to_fig_w(self, points: float) -> float:
        return points / 72 / self.fig.get_figwidth()

    # -- layout ------------------------------------------------------------------------------

    def header(self) -> float:
        """Draw title, subtitle and legend; return the figure y where the plot area may start."""
        spec = self.spec
        left = self.pt_to_fig_w(18)
        y = 1 - self.pt_to_fig_h(16)
        self.text(
            left, y, spec.title, size=self.fs_title, color=self.ink["primary"], weight="semibold", va="top"
        )
        y -= self.pt_to_fig_h(self.fs_title * 1.45)
        if spec.subtitle:
            self.text(left, y, spec.subtitle, size=self.fs_sub, color=self.ink["secondary"], va="top")
            y -= self.pt_to_fig_h(self.fs_sub * 1.6)
        if self.legend_entries():
            y -= self.pt_to_fig_h(4)
            self.draw_legend(left, y)
            y -= self.pt_to_fig_h(self.fs_label * 1.9)
        return y - self.pt_to_fig_h(6)

    def legend_entries(self) -> list[tuple[str, str]]:
        spec = self.spec
        if spec.chart_type == "donut":
            return [(label, self.colors[i % len(self.colors)]) for i, label in enumerate(self.donut_labels())]
        if len(spec.series) < 2:
            return []
        return [(s.name, self.colors[i]) for i, s in enumerate(spec.series)]

    def draw_legend(self, left: float, y: float) -> None:
        renderer = self.canvas.get_renderer()
        x = left
        swatch_w = self.pt_to_fig_w(self.fs_label * 0.9)
        for name, color in self.legend_entries():
            if self.spec.chart_type in {"line"}:
                self.fig.add_artist(
                    Line2D(
                        [x, x + swatch_w * 1.4],
                        [y - self.pt_to_fig_h(self.fs_label * 0.55)] * 2,
                        transform=self.fig.transFigure,
                        color=color,
                        linewidth=LINE_WIDTH,
                        solid_capstyle="round",
                    )
                )
                x += swatch_w * 1.4 + self.pt_to_fig_w(5)
            else:
                size_h = self.pt_to_fig_h(self.fs_label * 0.9)
                self.fig.add_artist(
                    FancyBboxPatch(
                        (x, y - size_h * 1.05),
                        swatch_w,
                        size_h,
                        boxstyle="round,pad=0,rounding_size=0.002",
                        transform=self.fig.transFigure,
                        facecolor=color,
                        edgecolor="none",
                    )
                )
                x += swatch_w + self.pt_to_fig_w(5)
            label = self.text(
                x, y, _truncate(name, 28), size=self.fs_label, color=self.ink["secondary"], va="top"
            )
            width = label.get_window_extent(renderer).width / self.fig.bbox.width
            x += width + self.pt_to_fig_w(14)

    def footer(self) -> float:
        """Draw the source note; return the lowest figure y the plot area may use."""
        bottom = self.pt_to_fig_h(12)
        if self.spec.source:
            self.text(
                self.pt_to_fig_w(18),
                bottom,
                self.spec.source,
                size=self.fs_note,
                color=self.ink["muted"],
                va="bottom",
            )
            bottom += self.pt_to_fig_h(self.fs_note * 1.8)
        return bottom

    # -- axes --------------------------------------------------------------------------------

    def style_axes(self, ax: Any, *, value_axis: Literal["x", "y"]) -> None:
        ax.set_facecolor("none")
        for side in ("top", "right", "left", "bottom"):
            ax.spines[side].set_visible(False)
        baseline = ax.spines["bottom" if value_axis == "y" else "left"]
        baseline.set_visible(True)
        baseline.set_color(self.ink["axis"])
        baseline.set_linewidth(0.8)
        ax.grid(axis=value_axis, color=self.ink["grid"], linewidth=0.6, linestyle="-")
        ax.set_axisbelow(True)
        ax.tick_params(
            axis="both",
            length=0,
            labelsize=self.fs_tick,
            labelcolor=self.ink["muted"],
            pad=5,
        )
        formatter = FuncFormatter(lambda v, _pos: self.fmt(v))
        if value_axis == "y":
            ax.yaxis.set_major_formatter(formatter)
            ax.yaxis.set_major_locator(MaxNLocator(nbins=5, min_n_ticks=3))
        else:
            ax.xaxis.set_major_formatter(formatter)
            ax.xaxis.set_major_locator(MaxNLocator(nbins=5, min_n_ticks=3))
        if self.spec.x_label:
            ax.set_xlabel(self.spec.x_label, fontsize=self.fs_label, color=self.ink["secondary"], labelpad=8)
            ax.xaxis.label.set_fontfamily(self.font)
        if self.spec.y_label:
            ax.set_ylabel(self.spec.y_label, fontsize=self.fs_label, color=self.ink["secondary"], labelpad=8)
            ax.yaxis.label.set_fontfamily(self.font)

    def finalize_tick_fonts(self, ax: Any) -> None:
        for label in [*ax.get_xticklabels(), *ax.get_yticklabels()]:
            label.set_fontfamily(self.font)

    def place_axes(self, ax: Any, top: float, bottom: float) -> None:
        """Size the axes so tick labels fit: measure, then reposition."""
        renderer = self.canvas.get_renderer()
        left = self.pt_to_fig_w(18)
        right = 1 - self.pt_to_fig_w(28)
        ax.set_position((left, bottom, right - left, max(0.1, top - bottom)))
        self.finalize_tick_fonts(ax)
        for _ in range(2):  # tick labels can change once the axes are resized
            bbox = ax.get_tightbbox(renderer)
            if bbox is None:
                break
            fig_w, fig_h = self.fig.bbox.width, self.fig.bbox.height
            pos = ax.get_position()
            extra_left = (pos.x0 * fig_w - bbox.x0) / fig_w
            extra_bottom = (pos.y0 * fig_h - bbox.y0) / fig_h
            x0 = left + max(0.0, extra_left)
            y0 = bottom + max(0.0, extra_bottom)
            ax.set_position((x0, y0, max(0.1, right - x0), max(0.1, top - y0)))
            self.finalize_tick_fonts(ax)

    # -- chart types -------------------------------------------------------------------------

    def render(self) -> None:
        top = self.header()
        bottom = self.footer()
        chart_type = self.spec.chart_type
        if chart_type == "donut":
            self.draw_donut(top, bottom)
        elif chart_type in {"line", "area"}:
            self.draw_lines(top, bottom, area=chart_type == "area")
        else:
            self.draw_bars(top, bottom)

    def x_ticks(self, ax: Any) -> None:
        labels = self.spec.labels
        n = len(labels)
        temporal = _parse_temporal(labels)
        positions = list(range(n))
        if temporal:
            dates, kind = temporal
            max_ticks = 7
            step = max(1, math.ceil(n / max_ticks))
            if kind == "month" and step > 1:
                step = next((s for s in (2, 3, 4, 6, 12, 24) if s >= step), step)
            chosen = positions[::step]
            multi_year = dates[0].year != dates[-1].year
            tick_labels = []
            last_year = None
            for i in chosen:
                show_year = kind != "year" and (multi_year and dates[i].year != last_year)
                if kind == "day" and not multi_year and i == chosen[0]:
                    show_year = True
                tick_labels.append(_temporal_tick_label(dates[i], kind, show_year=show_year))
                last_year = dates[i].year
            ax.set_xticks(chosen, tick_labels)
        else:
            ax.set_xticks(positions, [_truncate(str(label), 18) for label in labels])

    def draw_lines(self, top: float, bottom: float, *, area: bool) -> None:
        spec = self.spec
        ax = self.fig.add_axes((0, 0, 1, 1))
        self.style_axes(ax, value_axis="y")
        xs = list(range(len(spec.labels)))
        for index, series in enumerate(spec.series):
            color = self.colors[index]
            ys = [math.nan if v is None else float(v) for v in series.values]
            ax.plot(
                xs,
                ys,
                color=color,
                linewidth=LINE_WIDTH,
                solid_capstyle="round",
                solid_joinstyle="round",
                zorder=3,
            )
            if area:
                ax.fill_between(xs, ys, 0, color=color, alpha=0.10, linewidth=0, zorder=2)
            last = next((i for i in range(len(ys) - 1, -1, -1) if not math.isnan(ys[i])), None)
            if last is not None:
                ax.plot(
                    [xs[last]],
                    [ys[last]],
                    marker="o",
                    markersize=MARKER_SIZE * min(1.0, self.fs_tick / 8),
                    color=color,
                    markeredgecolor=self.ink["surface"],
                    markeredgewidth=1.5,
                    zorder=4,
                )
        self.x_ticks(ax)
        ax.set_xlim(-0.02 * max(1, len(xs) - 1), (len(xs) - 1) * 1.02 + (0.5 if len(xs) == 1 else 0))
        self.apply_value_limits(ax, include_zero=area)
        self.place_axes(ax, top, bottom)
        if spec.show_values in {"auto", "last", "all"}:
            self.label_line_ends(ax, all_points=spec.show_values == "all")

    def label_line_ends(self, ax: Any, *, all_points: bool) -> None:
        spec = self.spec
        # Only label line ends when they don't collide; otherwise the legend carries identity.
        ends: list[tuple[float, float, str]] = []
        for series in spec.series:
            values = series.values
            last = next((i for i in range(len(values) - 1, -1, -1) if values[i] is not None), None)
            if last is not None:
                value = values[last]
                assert value is not None
                ends.append((last, float(value), self.fmt(value)))
        if all_points and len(spec.series) == 1 and len(spec.labels) <= 16:
            ends = [(i, float(v), self.fmt(v)) for i, v in enumerate(spec.series[0].values) if v is not None]
        span = ax.get_ylim()[1] - ax.get_ylim()[0]
        ys = sorted(y for _, y, _ in ends)
        if any(b - a < span * 0.06 for a, b in itertools.pairwise(ys)) and not all_points:
            return
        for x, y, text in ends:
            ax.annotate(
                text,
                (x, y),
                xytext=(0, 7),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=self.fs_label,
                color=self.ink["primary"],
                fontfamily=self.font,
                weight="semibold",
                zorder=5,
            )

    def apply_value_limits(self, ax: Any, *, include_zero: bool, axis: Literal["x", "y"] = "y") -> None:
        spec = self.spec
        numbers = [v for s in spec.series for v in s.values if v is not None]
        if spec.chart_type == "stacked_bar":
            numbers = [sum(s.values[i] or 0 for s in spec.series) for i in range(len(spec.labels))] + [0.0]
        if not numbers:
            numbers = [0.0, 1.0]
        low, high = min(numbers), max(numbers)
        if include_zero:
            low, high = min(low, 0.0), max(high, 0.0)
        if math.isclose(low, high):
            high = low + (abs(low) or 1.0)
        pad = (high - low) * 0.12
        lower = spec.y_min if spec.y_min is not None else (low if include_zero and low >= 0 else low - pad)
        upper = spec.y_max if spec.y_max is not None else high + pad
        if axis == "y":
            ax.set_ylim(lower, upper)
        else:
            ax.set_xlim(lower, upper)

    def bar_colors(self, series_index: int) -> list[str]:
        spec = self.spec
        base = self.colors[series_index]
        if spec.highlight and len(spec.series) == 1:
            return [base if label == spec.highlight else self.ink["deemphasis"] for label in spec.labels]
        return [base] * len(spec.labels)

    def draw_bars(self, top: float, bottom: float) -> None:
        spec = self.spec
        horizontal = spec.chart_type == "horizontal_bar"
        stacked = spec.chart_type == "stacked_bar"
        labels = spec.labels
        n = len(labels)
        ax = self.fig.add_axes((0, 0, 1, 1))
        value_axis: Literal["x", "y"] = "x" if horizontal else "y"
        self.style_axes(ax, value_axis=value_axis)
        if horizontal:
            ax.set_yticks(range(n), [_truncate(str(label)) for label in labels])
            ax.set_ylim(n - 0.5, -0.5)  # first label on top
            self.apply_value_limits(ax, include_zero=True, axis="x")
        else:
            self.x_ticks(ax)
            ax.set_xlim(-0.5, n - 0.5)
            self.apply_value_limits(ax, include_zero=True, axis="y")
        self.place_axes(ax, top, bottom)

        # Convert point-based mark specs into data units now that the layout is final.
        origin = ax.transData.transform((0, 0))
        unit = ax.transData.transform((1, 1)) - origin
        px_per_pt = self.dpi / 72
        band_px = abs(unit[1] if horizontal else unit[0])
        value_px = abs(unit[0] if horizontal else unit[1])
        groups = 1 if stacked else len(spec.series)
        slot = 0.72 / groups
        thickness = min(slot, BAR_MAX_THICKNESS * px_per_pt / band_px)
        gap_band = SURFACE_GAP * px_per_pt / band_px
        gap_value = SURFACE_GAP * px_per_pt / value_px
        radius_band = BAR_RADIUS * px_per_pt / band_px
        radius_value = BAR_RADIUS * px_per_pt / value_px

        totals = [0.0] * n
        for s_index, series in enumerate(spec.series):
            colors = self.bar_colors(s_index)
            for i, raw in enumerate(series.values):
                if raw is None or raw == 0:
                    continue
                value = float(raw)
                center: float
                if stacked:
                    center = float(i)
                    base = totals[i] + (gap_value / 2 if totals[i] else 0)
                    end = totals[i] + value
                    totals[i] = end
                    is_top = all((later.values[i] or 0) == 0 for later in spec.series[s_index + 1 :])
                else:
                    offset = (s_index - (groups - 1) / 2) * slot
                    center = i + offset
                    base, end, is_top = 0.0, value, True
                half = thickness / 2 - (gap_band / 2 if groups > 1 else 0)
                x0, x1 = center - half, center + half
                if is_top and horizontal:
                    path = _rounded_bar_path(x0, x1, base, end, radius_value, radius_band, horizontal=True)
                elif is_top:
                    path = _rounded_bar_path(x0, x1, base, end, radius_band, radius_value, horizontal=False)
                else:
                    path = _square_bar_path(x0, x1, base, end - gap_value / 2, horizontal=horizontal)
                ax.add_patch(PathPatch(path, facecolor=colors[i], edgecolor="none", zorder=3))

        self.label_bars(ax, horizontal=horizontal, stacked=stacked, slot=slot, groups=groups)

    def label_bars(self, ax: Any, *, horizontal: bool, stacked: bool, slot: float, groups: int) -> None:
        spec = self.spec
        n = len(spec.labels)
        mode = spec.show_values
        if mode == "none":
            return
        if mode == "auto" and (n * groups > 16 or (stacked and n > 16)):
            return
        if stacked:
            totals = [float(sum(s.values[i] or 0 for s in spec.series)) for i in range(n)]
            points: list[tuple[float, float]] = [(float(i), total) for i, total in enumerate(totals)]
        else:
            points = []
            for s_index, series in enumerate(spec.series):
                for i, value in enumerate(series.values):
                    if value is None:
                        continue
                    points.append((i + (s_index - (groups - 1) / 2) * slot, float(value)))
            if mode == "last" and spec.series:
                points = points[-1:]
        for position, value in points:
            text = self.fmt(value)
            if horizontal:
                ax.annotate(
                    text,
                    (value, position),
                    xytext=(5 if value >= 0 else -5, 0),
                    textcoords="offset points",
                    ha="left" if value >= 0 else "right",
                    va="center",
                    fontsize=self.fs_label * (0.92 if groups > 1 else 1),
                    color=self.ink["primary"],
                    fontfamily=self.font,
                    zorder=5,
                )
            else:
                ax.annotate(
                    text,
                    (position, value),
                    xytext=(0, 4 if value >= 0 else -4),
                    textcoords="offset points",
                    ha="center",
                    va="bottom" if value >= 0 else "top",
                    fontsize=self.fs_label * (0.92 if groups > 1 else 1),
                    color=self.ink["primary"],
                    fontfamily=self.font,
                    zorder=5,
                )

    def donut_slices(self) -> list[tuple[str, float]]:
        values = [float(v or 0) for v in self.spec.series[0].values]
        pairs = list(zip(self.spec.labels, values, strict=True))
        if len(pairs) > MAX_DONUT_SLICES:
            ranked = sorted(pairs, key=lambda p: p[1], reverse=True)
            head = ranked[: MAX_DONUT_SLICES - 1]
            other = sum(v for _, v in ranked[MAX_DONUT_SLICES - 1 :])
            pairs = [*head, ("Other", other)]
            self.spec._warnings.append(
                f"Donut charts show at most {MAX_DONUT_SLICES} slices; the smallest were folded into 'Other'."
            )
        return pairs

    def donut_labels(self) -> list[str]:
        return [label for label, _ in self.donut_slices()]

    def draw_donut(self, top: float, bottom: float) -> None:
        slices = self.donut_slices()
        total = sum(v for _, v in slices)
        if total <= 0:
            raise ChartError("Donut values must add up to more than zero.")
        height = top - bottom
        fig_ratio = self.fig.get_figwidth() / self.fig.get_figheight()
        size_w = height / fig_ratio
        ax = self.fig.add_axes((0.5 - size_w / 2, bottom, size_w, height))
        ax.set_xlim(-1.35, 1.35)
        ax.set_ylim(-1.35, 1.35)
        ax.set_aspect("equal")
        ax.axis("off")
        start = 90.0
        ring = 0.30
        for index, (_label, value) in enumerate(slices):
            if value <= 0:
                continue
            sweep = value / total * 360
            color = self.colors[index % len(self.colors)]
            ax.add_patch(
                Wedge(
                    (0, 0),
                    1.0,
                    start - sweep,
                    start,
                    width=ring,
                    facecolor=color,
                    edgecolor=self.ink["surface"] if not self.spec.transparent else "none",
                    linewidth=SURFACE_GAP if not self.spec.transparent else 0,
                )
            )
            share = value / total * 100
            if self.spec.show_values != "none" and share >= 3:
                mid = math.radians(start - sweep / 2)
                ax.text(
                    1.17 * math.cos(mid),
                    1.17 * math.sin(mid),
                    f"{share:.0f}%",
                    ha="center",
                    va="center",
                    fontsize=self.fs_label,
                    color=self.ink["primary"],
                    fontfamily=self.font,
                )
            start -= sweep
        if self.spec.value_format != "percent":
            ax.text(
                0,
                0.07,
                self.fmt(total),
                ha="center",
                va="center",
                fontsize=self.fs_title * 1.3,
                color=self.ink["primary"],
                fontfamily=self.font,
                weight="semibold",
            )
            ax.text(
                0,
                -0.16,
                "total",
                ha="center",
                va="center",
                fontsize=self.fs_label,
                color=self.ink["muted"],
                fontfamily=self.font,
            )

    # -- output ------------------------------------------------------------------------------

    def save(self, path: Path, fmt: OutputFormat) -> None:
        self.fig.savefig(
            path,
            format=fmt,
            dpi=self.dpi,
            facecolor=self.fig.get_facecolor(),
            transparent=self.spec.transparent,
            metadata={"Creator": "youtube-studio-mcp"} if fmt in {"png", "pdf"} else None,
        )

    def preview_png(self, max_width: int = 1000) -> bytes:
        buffer = io.BytesIO()
        dpi = min(self.dpi, self.dpi * max_width / self.spec.width)
        self.fig.savefig(buffer, format="png", dpi=dpi, facecolor=self.fig.get_facecolor())
        return buffer.getvalue()


def render_chart(spec: ChartSpec, outputs: Sequence[tuple[Path, OutputFormat]]) -> ChartResult:
    """Render ``spec`` to every ``(path, format)`` in ``outputs`` and return a PNG preview."""
    _validate(spec)
    renderer = _Renderer(spec)
    renderer.render()
    written: list[Path] = []
    for path, fmt in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        renderer.save(path, fmt)
        written.append(path)
    preview = renderer.preview_png()
    return ChartResult(files=written, preview_png=preview, warnings=list(spec._warnings))
