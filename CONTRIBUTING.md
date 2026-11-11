# Contributing

Thanks for helping creators get more out of their data! Issues and pull requests are welcome.

## Setup

```bash
git clone https://github.com/nelsonrogers/youtube-studio-mcp && cd youtube-studio-mcp
uv sync
uv run pytest
```

You don't need a YouTube channel or Google credentials to develop: the test suite runs every tool
against `tests/fakes.py`, a deterministic fake of the YouTube Data, Analytics and Reporting APIs.
The fake also **rejects the query shapes the real Analytics API rejects** (e.g. a `video` dimension
without `sort`/`maxResults`, demographics with metrics other than `viewerPercentage`). If you add a
new report, encode its documented constraints in the fake too.

To try changes against your own channel, sign in (`uv run youtube-studio-mcp auth`) and run
`uv run youtube-studio-mcp check`, or open the MCP Inspector:
`npx @modelcontextprotocol/inspector uv run youtube-studio-mcp`.

## Before opening a PR

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
uv run python scripts/generate_tool_docs.py         # if you added/changed a tool
uv run python scripts/generate_sample_media_kit.py  # if you changed get_media_kit_data
```

## Guidelines

- **Tool descriptions are the UI.** Agents pick tools from their descriptions and parameter docs:
  say what the tool answers, units, and caveats. Keep tool count focused; prefer a parameter over
  a near-duplicate tool.
- **Never write to stdout** in server code (it carries JSON-RPC). Use `logging` (stderr).
- **Anticipated failures raise `ToolError`** (via `@tool_errors`) with an actionable message; the
  user is often non-technical.
- **Quote exact date ranges** in results; analytics lag and use Pacific time.
- **Media-kit contract:** within a `schema_version`, only add keys to `get_media_kit_data`.
- **Read-only by default.** Anything that changes a channel belongs in `tools/write.py` (opt-in),
  with a dry run or explicit confirmation guidance.
- **No secrets in issues, fixtures or logs.**
- Add an entry to `CHANGELOG.md` under *Unreleased*.

## Releasing (maintainers)

1. Update `version` in `pyproject.toml` and move *Unreleased* in `CHANGELOG.md` under the version.
2. Commit, tag `vX.Y.Z`, push the tag. The release workflow tests, builds and creates the GitHub
   release (and publishes to PyPI once enabled; see `.github/workflows/release.yml`).
