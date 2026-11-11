"""Regenerate docs/examples/media-kit-data.sample.json from the fake API (fictional channel).

Run from the repository root:  uv run python scripts/generate_sample_media_kit.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fakes import FakeYouTubeAPI  # noqa: E402

from youtube_studio_mcp import periods  # noqa: E402
from youtube_studio_mcp.config import Settings  # noqa: E402
from youtube_studio_mcp.context import AppContext  # noqa: E402
from youtube_studio_mcp.tools.media_kit import build_media_kit  # noqa: E402

TODAY = date(2026, 11, 11)


def main() -> None:
    periods.today_pacific = lambda: TODAY  # type: ignore[assignment]
    settings = Settings(config_dir=ROOT / ".sample-config", output_dir=ROOT / ".sample-out")
    ctx = AppContext(settings=settings, api=FakeYouTubeAPI(today=TODAY))
    snapshot = build_media_kit(ctx, ctx.resolve("last_28_days", None, None), recent_uploads_per_format=6)
    snapshot["generated_at"] = "2026-11-11T09:00:00+00:00"
    destination = ROOT / "docs" / "examples" / "media-kit-data.sample.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {destination.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
