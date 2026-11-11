"""Keep generated docs in sync with the code."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parent.parent


def test_tool_reference_is_up_to_date() -> None:
    spec = importlib.util.spec_from_file_location(
        "generate_tool_docs", ROOT / "scripts" / "generate_tool_docs.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = module.render(anyio.run(module.collect))
    current = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    assert current == expected, "docs/tools.md is stale: run `uv run python scripts/generate_tool_docs.py`"
