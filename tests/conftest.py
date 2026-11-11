from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from tests.fakes import FakeYouTubeAPI
from youtube_studio_mcp.config import Settings
from youtube_studio_mcp.context import AppContext
from youtube_studio_mcp.server import build_server

TODAY = date(2026, 11, 11)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never touch the developer's real config/tokens; pin "today" for date maths."""
    for name in list(__import__("os").environ):
        if name.startswith("YOUTUBE_STUDIO_MCP_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("youtube_studio_mcp.periods.today_pacific", lambda: TODAY)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config", output_dir=tmp_path / "out")


@pytest.fixture
def fake_api() -> FakeYouTubeAPI:
    return FakeYouTubeAPI(today=TODAY)


@pytest.fixture
def ctx(settings: Settings, fake_api: FakeYouTubeAPI) -> AppContext:
    return AppContext(settings=settings, api=fake_api)


@pytest.fixture
def make_client(settings: Settings) -> Callable[..., Any]:
    def factory(api: FakeYouTubeAPI | None = None, *, enable_write: bool = False) -> Client:
        configured = Settings(
            config_dir=settings.config_dir, output_dir=settings.output_dir, enable_write=enable_write
        )
        return Client(build_server(configured, api=api or FakeYouTubeAPI(today=TODAY)))

    return factory


@pytest.fixture
async def client(make_client: Callable[..., Client], fake_api: FakeYouTubeAPI) -> AsyncIterator[Client]:
    async with make_client(fake_api) as connected:
        yield connected
