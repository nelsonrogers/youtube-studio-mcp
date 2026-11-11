"""Chart rendering tool."""

from __future__ import annotations

import base64
import json
import re
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp_types import CallToolResult, ContentBlock, ImageContent, TextContent
from pydantic import BaseModel, Field

from youtube_studio_mcp.charts import (
    ChartSpec,
    ChartType,
    OutputFormat,
    SeriesData,
    ShowValues,
    Theme,
    ValueFormat,
    render_chart,
)
from youtube_studio_mcp.context import LOCAL_WRITE, AppContext, tool_errors


class SeriesInput(BaseModel):
    name: str = Field(description="Series name, shown in the legend when there are 2+ series.")
    values: list[float | None] = Field(description="One value per label (same order); null leaves a gap.")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:50] or "chart"


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(title="Create chart", annotations=LOCAL_WRITE)
    @tool_errors
    def create_chart(
        chart_type: Annotated[
            ChartType,
            Field(
                description=(
                    "line/area: trends over time. bar: compare categories (multiple series = grouped). "
                    "horizontal_bar: ranked lists with long labels (countries, videos, traffic sources). "
                    "stacked_bar: parts of a total per category. donut: share of one whole (max 6 slices)."
                )
            ),
        ],
        labels: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=1000,
                description="X-axis categories or dates. ISO dates (YYYY-MM-DD, YYYY-MM, YYYY) get smart date ticks.",
            ),
        ],
        series: Annotated[
            list[SeriesInput], Field(min_length=1, max_length=8, description="1-8 data series.")
        ],
        title: Annotated[str, Field(description="Chart title (the headline).")],
        subtitle: Annotated[
            str | None, Field(description="Smaller line under the title, e.g. the period.")
        ] = None,
        source_note: Annotated[
            str | None, Field(description="Footnote, e.g. 'Source: YouTube Analytics, Aug–Oct 2026'.")
        ] = None,
        x_label: str | None = None,
        y_label: str | None = None,
        value_format: Annotated[
            ValueFormat,
            Field(
                description=(
                    "How numbers print: compact (12.3K), percent (values already 0-100), duration "
                    "(values in seconds -> m:ss), currency, number, or auto."
                )
            ),
        ] = "auto",
        currency_symbol: str = "$",
        colors: Annotated[
            list[str] | None,
            Field(description="Brand colours as hex, used in order for series (or donut slices)."),
        ] = None,
        highlight: Annotated[
            str | None,
            Field(description="For a single-series bar chart: the label to emphasise; other bars are muted."),
        ] = None,
        theme: Annotated[Theme, Field(description="Light or dark background.")] = "light",
        transparent_background: Annotated[
            bool, Field(description="Transparent PNG/SVG background for placing on designed pages.")
        ] = False,
        width: Annotated[int, Field(ge=400, le=6000, description="Width in pixels.")] = 1600,
        height: Annotated[int, Field(ge=300, le=6000, description="Height in pixels.")] = 900,
        show_values: Annotated[
            ShowValues,
            Field(description="Value labels: auto (bars when few, line end points), all, last, or none."),
        ] = "auto",
        font_family: Annotated[
            str | None, Field(description="Installed font name, e.g. 'Montserrat'. Falls back if missing.")
        ] = None,
        y_min: Annotated[float | None, Field(description="Force the value-axis minimum.")] = None,
        y_max: Annotated[float | None, Field(description="Force the value-axis maximum.")] = None,
        filename: Annotated[
            str | None,
            Field(
                description=(
                    "Where to save. Bare names go to the output directory; absolute paths allowed. The "
                    "extension (.png/.svg/.pdf) picks the format. Default: <title>-<timestamp>.png."
                )
            ),
        ] = None,
        extra_formats: Annotated[
            list[OutputFormat] | None,
            Field(description="Also save these formats next to the main file, e.g. ['svg'] for print."),
        ] = None,
        include_preview: Annotated[
            bool, Field(description="Return a small PNG preview so you can check the chart visually.")
        ] = True,
    ) -> CallToolResult:
        """Render a polished, media-kit-ready chart (PNG, SVG or PDF) from data you provide —
        typically numbers from the analytics tools. Uses a colour-blind-safe palette by default,
        thin marks, clean axes and direct value labels. Returns the saved file path(s) and a
        preview image."""
        main = ctx.output_path(
            filename,
            default_stem=_slug(title),
            allowed=(".png", ".svg", ".pdf"),
            default_suffix=".png",
        )
        main_format: OutputFormat = main.suffix.lower().lstrip(".")  # type: ignore[assignment]
        formats: list[OutputFormat] = [main_format]
        for fmt in extra_formats or []:
            if fmt not in formats:
                formats.append(fmt)
        spec = ChartSpec(
            chart_type=chart_type,
            labels=[str(label) for label in labels],
            series=[SeriesData(s.name, list(s.values)) for s in series],
            title=title,
            subtitle=subtitle,
            x_label=x_label,
            y_label=y_label,
            value_format=value_format,
            currency_symbol=currency_symbol,
            colors=colors,
            theme=theme,
            transparent=transparent_background,
            width=width,
            height=height,
            show_values=show_values,
            highlight=highlight,
            source=source_note,
            font_family=font_family,
            y_min=y_min,
            y_max=y_max,
        )
        outputs = [(main.with_suffix(f".{fmt}"), fmt) for fmt in formats]
        result = render_chart(spec, outputs)
        info: dict[str, Any] = {"files": [str(path) for path in result.files], "warnings": result.warnings}
        content: list[ContentBlock] = [TextContent(type="text", text=json.dumps(info, indent=2))]
        if include_preview:
            content.append(
                ImageContent(
                    type="image", data=base64.b64encode(result.preview_png).decode(), mime_type="image/png"
                )
            )
        return CallToolResult(content=content, structured_content=info)
