"""YouTube Studio MCP: a local Model Context Protocol server for YouTube creators."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("youtube-studio-mcp")
except PackageNotFoundError:  # pragma: no cover - only when running from a source tree without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
