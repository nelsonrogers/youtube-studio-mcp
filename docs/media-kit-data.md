# Media-kit data contract (`schema_version` 1)

`get_media_kit_data` (MCP tool) and `youtube-studio-mcp media-kit` (CLI) return one JSON snapshot
with everything a creator media kit or sponsor deck usually shows. Templates can consume it
verbatim.

**Stability promise:** within a `schema_version`, keys are only ever *added*, never renamed or
removed. A breaking change bumps the version and is announced in the CHANGELOG. The tests compare
the code's output with [`examples/media-kit-data.sample.json`](examples/media-kit-data.sample.json)
(a fictional channel), so the sample always has the exact current shape.

## Conventions

- Keys are `snake_case`. Percentages (`*_pct`) are **0-100**. Durations ending in `_s` are
  **seconds**. Dates are ISO `YYYY-MM-DD` (US Pacific, YouTube's reporting timezone). Timestamps
  are ISO 8601 UTC. Numbers are raw (format them in the template).
- A **metric** is `{ "value", "previous", "change_pct" }`: the period, the previous period of the
  same length, and the % change (`null` when not computable).
- Anything YouTube can't provide is `null`/empty, with a human-readable reason under
  `unavailable.<key>`. The call itself only fails if the channel can't be read at all.
- Default period: `last_28_days`, ending at the latest complete day of analytics (`data_through`,
  usually 3 days before today). Studio's live numbers will therefore be slightly higher.

## Fields

| Key | Description |
|---|---|
| `schema_version` | `"1"` |
| `generated_at` | When the snapshot was made (UTC). |
| `data_through` | Last day included (= `period.end_date`). |
| `period`, `comparison_period` | `{start_date, end_date, days, label}`. |
| `channel` | `id, title, handle, url, description, created_at, country, thumbnail_url, subscribers, subscribers_is_rounded, subscribers_from_analytics, total_views, video_count`. `subscribers` is YouTube's public count, **rounded down to 3 significant figures** above 1,000 (48,409 → 48,400). The API offers nothing more precise. `subscribers_from_analytics` = lifetime gained − lost through `data_through`: exact, but a few days old. |
| `highlights` | Convenience headline values (subscribers, period views/watch time/net subscribers, engagement rate, average views of recent videos and Shorts, top country, top age group, female/male %, mobile %, Shorts view share, subscriber growth % over `monthly_trend`). |
| `performance` | Metrics: `views, engaged_views, watch_time_hours, average_view_duration_s, average_view_percentage, subscribers_gained, subscribers_lost, net_subscribers, likes, comments, shares, engagement_rate_pct` (+ revenue keys with `include_revenue`). `engagement_rate_pct = (likes + comments + shares) / views`. |
| `performance_by_content_type` | `{videos, shorts}`, each with metrics `views, engaged_views, likes, net_subscribers, watch_time_hours, average_view_duration_s, impressions, impressions_ctr_pct` (YouTube's own `creatorContentType` classification). |
| `impressions_funnel` | `impressions, impressions_ctr_pct, views_from_impressions` (= impressions × CTR), plus `average_view_duration_s` and `watch_hours_from_impressions`, which have no API source and are always `null`. Impressions need the reporting job (created by `youtube-studio-mcp auth`; data ~48 h later, with 30 days of history). |
| `daily_trend` | One row per day of the period: `{date, views, views_videos, views_shorts, views_live}` (`views` is the sum of the three). |
| `monthly_trend` | Last 12 months: `{period: "YYYY-MM", views, watch_hours, subscribers_gained, subscribers_lost, net_subscribers, total_subscribers, partial}`. `partial` flags months cut by the window. |
| `audience.age` | Always 7 rows: `{code: "age13-17"…"age65-", label: "13-17"…"65+", viewer_pct}` (share of logged-in viewers). Empty if YouTube withholds the data. |
| `audience.gender` | Always 3 rows: `{code: female\|male\|user_specified, label, viewer_pct}`. |
| `audience.countries` | Top 8: `{code (ISO 3166-1 alpha-2), views, share_pct}` (share of views with a known country). |
| `audience.devices`, `subscribed_status`, `content_type` | `{code, label, views, share_pct}`. Codes: `MOBILE, TV, DESKTOP, TABLET, GAME_CONSOLE, …`; `SUBSCRIBED, UNSUBSCRIBED`; `VIDEO_ON_DEMAND, SHORTS, LIVE_STREAM, …`. |
| `recent_uploads` | `{min_age_days, videos, shorts}`; each format is `{count, avg_views, median_views, avg_engagement_pct, items[]}`. Items are the newest public uploads at least `min_age_days` old (default 7), newest first: `{video_id, title, url, published_at, thumbnail_url, duration_s, views, likes, comments, engagement_rate_pct}` (lifetime public counts). |
| `top_videos` | Top 5 by views in the period: `{rank, video_id, title, url, thumbnail_url, published_at, content_type, views, watch_time_hours, average_view_duration_s, likes, comments, engagement_rate_pct}`. |
| `traffic_sources` | Top 8: `{code, label, views, share_pct}`. Codes are YouTube's `insightTrafficSourceType` values. Studio's "Browse features" is `SUBSCRIBER`; others include `SHORTS, YT_SEARCH, RELATED_VIDEO, YT_CHANNEL, NO_LINK_OTHER, EXT_URL, PLAYLIST, NOTIFICATION, END_SCREEN, HASHTAGS`. |
| `unavailable` | `{key: reason}`. Always contains `views_last_48_hours` (no API has realtime/hourly data) and `impressions_funnel.watch_metrics`. |
| `notes` | Human-readable caveats. |

## Producing it

```bash
# From any terminal (no AI client needed), e.g. monthly:
youtube-studio-mcp media-kit --out ~/Documents/media-kit/media-kit-data.json

# Other windows:
youtube-studio-mcp media-kit --period last_90_days --out kit-90d.json
youtube-studio-mcp media-kit --start-date 2026-10-01 --end-date 2026-10-31 --out october.json
```

From an agent: *"Call get_media_kit_data and save it to media-kit/media-kit-data.json"* (the
`save_to` argument).
