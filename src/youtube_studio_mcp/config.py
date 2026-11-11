"""Settings and file locations, resolved from environment variables.

Every setting can be provided through an environment variable prefixed with
``YOUTUBE_STUDIO_MCP_`` so that MCP clients (Claude Desktop, Claude Code, Cursor, ...)
can configure the server from their JSON config without command-line flags.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "YOUTUBE_STUDIO_MCP_"
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TRUTHY = {"1", "true", "yes", "on"}


def env(name: str) -> str | None:
    """Return a stripped ``YOUTUBE_STUDIO_MCP_<name>`` value, or None when unset/blank."""
    value = os.environ.get(ENV_PREFIX + name)
    if value is None or not value.strip():
        return None
    return value.strip()


def env_flag(name: str) -> bool:
    value = env(name)
    return value is not None and value.lower() in _TRUTHY


def default_config_dir() -> Path:
    """Where the OAuth client secret and tokens live.

    ``%APPDATA%\\youtube-studio-mcp`` on Windows, ``$XDG_CONFIG_HOME/youtube-studio-mcp``
    (default ``~/.config/youtube-studio-mcp``) everywhere else.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "youtube-studio-mcp"


def default_output_dir() -> Path:
    """Where charts and exports are written when the caller gives a bare filename."""
    documents = Path.home() / "Documents"
    return (documents if documents.is_dir() else Path.home()) / "youtube-studio-mcp"


def validate_profile(profile: str) -> str:
    if not _PROFILE_RE.match(profile):
        raise ValueError(
            f"Invalid profile name {profile!r}: use 1-64 letters, digits, '-' or '_' (e.g. 'main' or 'gaming')."
        )
    return profile


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration."""

    config_dir: Path = field(default_factory=default_config_dir)
    output_dir: Path = field(default_factory=default_output_dir)
    profile: str = "default"
    client_secret_file: Path | None = None
    client_id: str | None = None
    client_secret: str | None = None
    enable_write: bool = False

    @property
    def token_file(self) -> Path:
        return self.config_dir / "tokens" / f"{self.profile}.json"

    @property
    def default_client_secret_file(self) -> Path:
        return self.config_dir / "client_secret.json"

    @classmethod
    def from_env(cls, *, profile: str | None = None, enable_write: bool | None = None) -> Settings:
        config_dir = env("HOME")
        output_dir = env("OUTPUT_DIR")
        client_secret_file = env("CLIENT_SECRET_FILE")
        return cls(
            config_dir=Path(config_dir).expanduser() if config_dir else default_config_dir(),
            output_dir=Path(output_dir).expanduser() if output_dir else default_output_dir(),
            profile=validate_profile(profile or env("PROFILE") or "default"),
            client_secret_file=Path(client_secret_file).expanduser() if client_secret_file else None,
            client_id=env("CLIENT_ID"),
            client_secret=env("CLIENT_SECRET"),
            enable_write=env_flag("ENABLE_WRITE") if enable_write is None else enable_write,
        )
