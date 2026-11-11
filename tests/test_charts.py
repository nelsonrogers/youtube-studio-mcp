from pathlib import Path

import pytest

from youtube_studio_mcp.charts import ChartError, ChartSpec, SeriesData, format_value, render_chart

MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04"]


@pytest.mark.parametrize(
    ("value", "fmt", "expected"),
    [
        (1234, "number", "1,234"),
        (12345, "auto", "12.3K"),
        (2_500_000, "compact", "2.5M"),
        (999, "compact", "999"),
        (35.24, "percent", "35%"),
        (3.24, "percent", "3.2%"),
        (125, "duration", "2:05"),
        (3725, "duration", "1:02:05"),
        (12.5, "currency", "$12.50"),
        (-50, "currency", "-$50"),
        (None, "number", ""),
    ],
)
def test_format_value(value: float | None, fmt: str, expected: str) -> None:
    assert format_value(value, fmt) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "spec",
    [
        ChartSpec(
            "line", MONTHS, [SeriesData("Views", [1, 2, None, 4])], "Views", subtitle="s", source="src"
        ),
        ChartSpec("area", MONTHS, [SeriesData("A", [1, 2, 3, 4]), SeriesData("B", [2, 3, 4, 5])], "Area"),
        ChartSpec("bar", ["a", "b", "c"], [SeriesData("x", [3, -1, 2])], "Bars", highlight="a"),
        ChartSpec(
            "bar", ["a", "b"], [SeriesData("x", [3, 1]), SeriesData("y", [2, 2])], "Grouped", theme="dark"
        ),
        ChartSpec("horizontal_bar", ["United States", "France"], [SeriesData("v", [10, 4])], "Countries"),
        ChartSpec(
            "stacked_bar", MONTHS, [SeriesData("a", [1, 2, 0, 4]), SeriesData("b", [1, 1, 1, None])], "Stack"
        ),
        ChartSpec(
            "donut", list("abcdefgh"), [SeriesData("s", [8, 7, 6, 5, 4, 3, 2, 1])], "Donut", transparent=True
        ),
        ChartSpec(
            "line", [f"2026-01-{d:02d}" for d in range(1, 32)], [SeriesData("v", list(range(31)))], "Daily"
        ),
    ],
    ids=["line", "area", "bar-highlight", "grouped-dark", "hbar", "stacked", "donut-fold", "daily"],
)
def test_every_chart_type_renders(spec: ChartSpec, tmp_path: Path) -> None:
    outputs = [(tmp_path / "c.png", "png"), (tmp_path / "c.svg", "svg")]
    result = render_chart(spec, outputs)  # type: ignore[arg-type]
    assert all(path.stat().st_size > 1000 for path in result.files)
    assert result.preview_png.startswith(b"\x89PNG")
    assert (tmp_path / "c.svg").read_text().lstrip().startswith("<?xml")


def test_donut_folds_extra_slices_with_warning(tmp_path: Path) -> None:
    spec = ChartSpec("donut", list("abcdefgh"), [SeriesData("s", [8, 7, 6, 5, 4, 3, 2, 1])], "Donut")
    result = render_chart(spec, [(tmp_path / "d.png", "png")])
    assert any("Other" in warning for warning in result.warnings)


def test_missing_font_falls_back_with_warning(tmp_path: Path) -> None:
    spec = ChartSpec("bar", ["a"], [SeriesData("x", [1])], "T", font_family="Definitely Not A Font")
    result = render_chart(spec, [(tmp_path / "f.png", "png")])
    assert any("not installed" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (ChartSpec("line", ["a", "b"], [SeriesData("x", [1])], "T"), "must match"),
        (ChartSpec("donut", ["a"], [SeriesData("x", [1]), SeriesData("y", [1])], "T"), "exactly one series"),
        (ChartSpec("donut", ["a"], [SeriesData("x", [-1])], "T"), "negative"),
        (ChartSpec("stacked_bar", ["a"], [SeriesData("x", [-1])], "T"), "negative"),
        (ChartSpec("line", ["a"], [SeriesData(str(i), [1]) for i in range(9)], "T"), "At most 8"),
        (ChartSpec("line", ["a"], [SeriesData("x", [1])], "T", colors=["notacolor"]), "valid colour"),
        (ChartSpec("line", ["a"], [SeriesData("x", [1])], "T", width=100), "width"),
        (ChartSpec("line", [], [SeriesData("x", [])], "T"), "at least one"),
    ],
)
def test_invalid_specs(spec: ChartSpec, message: str, tmp_path: Path) -> None:
    with pytest.raises(ChartError, match=message):
        render_chart(spec, [(tmp_path / "x.png", "png")])
