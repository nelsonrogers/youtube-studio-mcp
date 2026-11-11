## What and why

<!-- What does this change, and which creator workflow does it help? -->

## Checklist

- [ ] `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy` pass
- [ ] New/changed tools have clear descriptions (agents read them) and tests against the fake API
- [ ] Docs updated (`uv run python scripts/generate_tool_docs.py` if tools changed)
- [ ] `get_media_kit_data` output only gained keys (no renames/removals within a schema version)
- [ ] CHANGELOG.md updated
