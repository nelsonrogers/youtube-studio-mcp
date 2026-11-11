# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-11

First release.

### Added

- Local stdio MCP server for the authenticated creator's channel (YouTube Data API v3, YouTube
  Analytics API v2, YouTube Reporting API).
- Browser sign-in that is safe for stdio (`youtube-studio-mcp auth` and the `authenticate` tool):
  loopback OAuth with PKCE, per-profile token storage, actionable error messages.
- Analytics tools: performance summary with period comparison, metrics over time (day/week/month/
  year with exact weighted aggregation and running subscriber totals), top videos with YouTube's
  content type, audience breakdowns (age, gender, geography, devices, subscriber status, Shorts vs
  long-form, playback location, sharing), traffic sources with search terms, audience retention
  with drop-off detection, revenue, and raw `query_analytics`.
- Impressions & CTR (beta) via the Reporting API, with cached daily reports.
- `get_media_kit_data` and `youtube-studio-mcp media-kit`: a versioned media-kit JSON contract
  (schema 1) with daily/monthly trends, per-format performance, impressions funnel and audience.
- `create_chart`: PNG/SVG/PDF charts with a colour-blind-safe palette, brand colours, dark mode and
  transparent backgrounds.
- Content tools: channel overview, videos, playlists, comments (including the review queue).
- Opt-in write tools: update video details (dry-run diff), reply to comments, moderate comments.
- Prompt templates: channel check-up, media-kit refresh, video deep dive, comment digest.
- CLI: `serve`, `auth`, `status`, `check`, `media-kit`, `logout`.

[Unreleased]: https://github.com/nelsonrogers/youtube-studio-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nelsonrogers/youtube-studio-mcp/releases/tag/v0.1.0
