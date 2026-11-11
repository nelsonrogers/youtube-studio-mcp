"""Render docs/images/example-charts.png (README) from the sample media-kit snapshot.

Run from the repository root:  uv run python scripts/generate_readme_image.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from PIL import Image

from youtube_studio_mcp.charts import ChartSpec, SeriesData, render_chart

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "docs" / "examples" / "media-kit-data.sample.json"
DESTINATION = ROOT / "docs" / "images" / "example-charts.png"
COUNTRY_NAMES = {
    "US": "United States",
    "FR": "France",
    "CA": "Canada",
    "GB": "United Kingdom",
    "BE": "Belgium",
}


def main() -> None:
    kit = json.loads(SAMPLE.read_text(encoding="utf-8"))
    note = "Fictional sample channel · YouTube Analytics"
    monthly = [row for row in kit["monthly_trend"] if not row["partial"]]
    age = kit["audience"]["age"]
    top_age = max(age, key=lambda a: a["viewer_pct"])["label"]
    countries = kit["audience"]["countries"][:5]
    specs = [
        ChartSpec(
            "line",
            [row["period"] for row in monthly],
            [SeriesData("Subscribers", [row["total_subscribers"] for row in monthly])],
            "Subscriber growth",
            subtitle="Total subscribers at the end of each month",
            source=note,
        ),
        ChartSpec(
            "bar",
            [a["label"] for a in age],
            [SeriesData("Viewers", [a["viewer_pct"] for a in age])],
            "Audience age",
            subtitle="Share of viewers, last 28 days",
            value_format="percent",
            highlight=top_age,
            source=note,
        ),
        ChartSpec(
            "horizontal_bar",
            [COUNTRY_NAMES.get(c["code"], c["code"]) for c in countries],
            [SeriesData("Share", [c["share_pct"] for c in countries])],
            "Top countries",
            subtitle="Share of views, last 28 days",
            value_format="percent",
            source=note,
        ),
        ChartSpec(
            "area",
            [d["date"] for d in kit["daily_trend"]],
            [
                SeriesData("Shorts", [d["views_shorts"] for d in kit["daily_trend"]]),
                SeriesData("Videos", [d["views_videos"] for d in kit["daily_trend"]]),
            ],
            "Daily views by format",
            subtitle="Last 28 days",
            source=note,
        ),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tiles = []
        for index, spec in enumerate(specs):
            path = Path(tmp) / f"{index}.png"
            render_chart(spec, [(path, "png")])
            tiles.append(Image.open(path).convert("RGB"))
        width, height = tiles[0].size
        gap = 24
        sheet = Image.new("RGB", (width * 2 + gap * 3, height * 2 + gap * 3), "#eeede8")
        for index, tile in enumerate(tiles):
            sheet.paste(tile, (gap + (index % 2) * (width + gap), gap + (index // 2) * (height + gap)))
        sheet = sheet.resize((1640, round(1640 * sheet.height / sheet.width)), Image.Resampling.LANCZOS)
        DESTINATION.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(DESTINATION, optimize=True)
    print(f"Wrote {DESTINATION.relative_to(ROOT)} ({DESTINATION.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
