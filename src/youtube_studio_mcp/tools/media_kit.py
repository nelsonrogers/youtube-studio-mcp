"""One-call snapshot of the numbers creators put in a media kit / sponsor deck.

The output is a documented, versioned contract (``schema_version``): templates consume it
verbatim, so keys are only ever added, never renamed or removed, within a major version.
See docs/media-kit-data.md.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from mcp.server import MCPServer
from pydantic import Field

from youtube_studio_mcp.analytics import (
    aggregate_totals,
    label_for,
    pct_change,
    run_report_with_optional_metrics,
)
from youtube_studio_mcp.context import READ_ONLY, AppContext, EndDateArg, PeriodArg, StartDateArg, tool_errors
from youtube_studio_mcp.periods import DateRange, PeriodError, add_months
from youtube_studio_mcp.tools.analytics import (
    breakdown,
    classify_videos,
    performance_summary,
    timeseries,
    top_videos,
    totals,
    traffic_sources,
)
from youtube_studio_mcp.tools.impressions import ReachData, load_reach
from youtube_studio_mcp.youtube import (
    YouTubeAPIError,
    get_videos,
    list_playlist_video_ids,
    summarize_video,
    to_int,
)

SCHEMA_VERSION = "1"

PERFORMANCE_KEYS = {
    "views": "views",
    "engaged_views": "engagedViews",
    "watch_time_hours": "watchTimeHours",
    "average_view_duration_s": "averageViewDuration",
    "average_view_percentage": "averageViewPercentage",
    "subscribers_gained": "subscribersGained",
    "subscribers_lost": "subscribersLost",
    "net_subscribers": "netSubscribers",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "engagement_rate_pct": "engagementRatePct",
}
REVENUE_KEYS = {
    "estimated_revenue": "estimatedRevenue",
    "estimated_ad_revenue": "estimatedAdRevenue",
    "gross_revenue": "grossRevenue",
    "rpm": "rpm",
    "cpm": "cpm",
    "playback_based_cpm": "playbackBasedCpm",
    "monetized_playbacks": "monetizedPlaybacks",
    "ad_impressions": "adImpressions",
}
CONTENT_TYPES = {"videos": "VIDEO_ON_DEMAND", "shorts": "SHORTS"}
CONTENT_TYPE_METRICS = [
    "views",
    "likes",
    "subscribersGained",
    "subscribersLost",
    "estimatedMinutesWatched",
    "averageViewDuration",
]
AGE_CODES = ["age13-17", "age18-24", "age25-34", "age35-44", "age45-54", "age55-64", "age65-"]
GENDER_CODES = ["female", "male", "user_specified"]


def _section(unavailable: dict[str, str], name: str, fn: Callable[[], Any]) -> Any:
    """Run one section; record (instead of raising) recoverable API failures."""
    try:
        return fn()
    except (YouTubeAPIError, PeriodError, ValueError) as exc:
        unavailable[name] = str(exc).splitlines()[0]
        return None


def _metric(value: Any, previous: Any) -> dict[str, Any]:
    return {"value": value, "previous": previous, "change_pct": pct_change(value, previous)}


def _rows(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((result or {}).get("rows") or [])


def _hours(minutes: Any) -> float | None:
    return None if minutes is None else round(float(minutes) / 60, 1)


# --------------------------------------------------------------------------- content types


def content_type_daily(ctx: AppContext, date_range: DateRange) -> list[dict[str, Any]]:
    """Daily rows split by ``creatorContentType`` (one query feeds the trend and the totals)."""
    report = run_report_with_optional_metrics(
        ctx.api,
        date_range,
        CONTENT_TYPE_METRICS,
        ("engagedViews",),
        dimensions=["day", "creatorContentType"],
        sort="day",
    )
    return report.rows


def content_type_totals(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    metrics = [*CONTENT_TYPE_METRICS, "engagedViews"]
    grouped = aggregate_totals(rows, metrics, group_by="creatorContentType")
    out: dict[str, dict[str, Any]] = {}
    for key, api_type in CONTENT_TYPES.items():
        values = grouped.get(api_type) or {}
        has_engaged = any("engagedViews" in row for row in rows)
        out[key] = {
            "views": values.get("views", 0),
            "engaged_views": values.get("engagedViews", 0) if has_engaged else None,
            "likes": values.get("likes", 0),
            "net_subscribers": int(values.get("subscribersGained") or 0)
            - int(values.get("subscribersLost") or 0),
            "watch_time_hours": _hours(values.get("estimatedMinutesWatched", 0)),
            "average_view_duration_s": values.get("averageViewDuration"),
        }
    return out


def daily_trend(rows: list[dict[str, Any]], date_range: DateRange) -> list[dict[str, Any]]:
    per_day: dict[str, dict[str, Any]] = {}
    day = date_range.start
    while day <= date_range.end:
        per_day[day.isoformat()] = {
            "date": day.isoformat(),
            "views": 0,
            "views_videos": 0,
            "views_shorts": 0,
            "views_live": 0,
            "views_other": 0,  # posts, stories, unclassified
        }
        day += timedelta(days=1)
    column = {"VIDEO_ON_DEMAND": "views_videos", "SHORTS": "views_shorts", "LIVE_STREAM": "views_live"}
    for row in rows:
        entry = per_day.get(str(row.get("day")))
        if entry is None:
            continue
        views = int(row.get("views") or 0)
        entry["views"] += views
        entry[column.get(str(row.get("creatorContentType")), "views_other")] += views
    return list(per_day.values())


# --------------------------------------------------------------------------- uploads


def upload_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    views = [int(v["views"]) for v in items if v.get("views") is not None]
    engagement = [float(v["engagement_rate_pct"]) for v in items if v.get("engagement_rate_pct") is not None]
    return {
        "count": len(items),
        "avg_views": round(statistics.fmean(views)) if views else None,
        "median_views": round(statistics.median(views)) if views else None,
        "avg_engagement_pct": round(statistics.fmean(engagement), 2) if engagement else None,
        "items": [
            {
                "video_id": v["video_id"],
                "title": v.get("title"),
                "url": v.get("url"),
                "published_at": v.get("published_at"),
                "thumbnail_url": v.get("thumbnail_url"),
                "duration_s": v.get("duration_seconds"),
                "views": v.get("views"),
                "likes": v.get("likes"),
                "comments": v.get("comments"),
                "engagement_rate_pct": v.get("engagement_rate_pct"),
            }
            for v in items
        ],
    }


def recent_uploads(ctx: AppContext, latest: date, *, per_format: int, min_age_days: int) -> dict[str, Any]:
    ids = list_playlist_video_ids(ctx.api, ctx.uploads_playlist_id(), max(60, per_format * 6))
    cutoff = (datetime.now(UTC) - timedelta(days=min_age_days)).strftime("%Y-%m-%dT%H:%M:%S")
    videos = [
        summarize_video(item)
        for item in get_videos(
            ctx.api, ids, parts="snippet,contentDetails,statistics,status,liveStreamingDetails"
        )
    ]
    videos = [
        v
        for v in videos
        if v.get("privacy_status") == "public" and str(v.get("published_at") or "")[:19] <= cutoff
    ]
    videos.sort(key=lambda v: str(v.get("published_at") or ""), reverse=True)
    types: dict[str, str] = {}
    if videos:
        earliest = date.fromisoformat(min(str(v.get("published_at"))[:10] for v in videos))
        try:
            types = classify_videos(ctx, DateRange(min(earliest, latest), latest, "uploads"))
        except (YouTubeAPIError, PeriodError):
            types = {}
    fallback = {"short": "SHORTS", "live": "LIVE_STREAM"}

    def kind(video: dict[str, Any]) -> str:
        return types.get(str(video["video_id"])) or fallback.get(str(video.get("format")), "VIDEO_ON_DEMAND")

    return {
        "min_age_days": min_age_days,
        "videos": upload_stats([v for v in videos if kind(v) == "VIDEO_ON_DEMAND"][:per_format]),
        "shorts": upload_stats([v for v in videos if kind(v) == "SHORTS"][:per_format]),
    }


# --------------------------------------------------------------------------- builder


def build_media_kit(
    ctx: AppContext,
    date_range: DateRange,
    *,
    trend_months: int = 12,
    recent_uploads_per_format: int = 10,
    min_upload_age_days: int = 7,
    top_countries: int = 8,
    top_videos_limit: int = 5,
    include_revenue: bool = False,
) -> dict[str, Any]:
    unavailable: dict[str, str] = {
        "views_last_48_hours": "YouTube's APIs have no realtime or hourly data (Studio's 48-hour card).",
    }
    previous_range = date_range.previous()

    # -- channel ------------------------------------------------------------------------------
    channel = ctx.channel(fresh=True)
    snippet = channel.get("snippet") or {}
    stats = channel.get("statistics") or {}
    handle = snippet.get("customUrl")
    thumbnails = snippet.get("thumbnails") or {}
    lifetime = _section(
        unavailable,
        "channel.subscribers_from_analytics",
        lambda: totals(
            ctx,
            DateRange(min(ctx.channel_start(), date_range.end), date_range.end, "lifetime"),
            ("subscribersGained", "subscribersLost"),
        ),
    )
    lifetime_net = (
        int(lifetime.get("subscribersGained") or 0) - int(lifetime.get("subscribersLost") or 0)
        if lifetime is not None
        else None
    )
    subscribers = to_int(stats.get("subscriberCount"))
    channel_info = {
        "id": channel.get("id"),
        "title": snippet.get("title"),
        "handle": handle,
        "url": f"https://www.youtube.com/{handle}"
        if handle
        else f"https://www.youtube.com/channel/{channel.get('id')}",
        "description": snippet.get("description"),
        "created_at": snippet.get("publishedAt"),
        "country": snippet.get("country"),
        "thumbnail_url": (thumbnails.get("high") or thumbnails.get("default") or {}).get("url"),
        "subscribers": subscribers,
        "subscribers_is_rounded": bool(subscribers and subscribers >= 1000),
        "subscribers_from_analytics": lifetime_net,
        "total_views": to_int(stats.get("viewCount")),
        "video_count": to_int(stats.get("videoCount")),
    }

    # -- performance --------------------------------------------------------------------------
    summary = performance_summary(ctx, date_range, compare=True, include_revenue=include_revenue)
    if summary.get("revenue_unavailable"):
        unavailable["revenue"] = summary["revenue_unavailable"]
    metrics = summary["metrics"]
    keys = {**PERFORMANCE_KEYS, **(REVENUE_KEYS if include_revenue else {})}
    performance = {
        key: {
            "value": (metrics.get(api) or {}).get("value"),
            "previous": (metrics.get(api) or {}).get("previous"),
            "change_pct": (metrics.get(api) or {}).get("change_pct"),
        }
        for key, api in keys.items()
    }

    # -- content types, daily trend, impressions ----------------------------------------------
    current_rows = _section(unavailable, "daily_trend", lambda: content_type_daily(ctx, date_range))
    previous_rows = _section(
        unavailable, "performance_by_content_type.previous", lambda: content_type_daily(ctx, previous_range)
    )
    trend_daily = daily_trend(current_rows, date_range) if current_rows is not None else []
    current_ct = content_type_totals(current_rows) if current_rows is not None else None
    previous_ct = content_type_totals(previous_rows) if previous_rows is not None else None
    if current_rows is None:
        unavailable.setdefault("performance_by_content_type", unavailable["daily_trend"])

    reach_before: ReachData | None = None
    video_types: dict[str, str] = {}
    reach_now: ReachData | None = _section(unavailable, "impressions", lambda: load_reach(ctx, date_range))
    if "impressions" not in unavailable:
        if reach_now is None:
            unavailable["impressions"] = (
                "Impressions reports are not enabled. Run `youtube-studio-mcp auth` again or call "
                "enable_impressions_reports once; YouTube publishes data ~48 hours later (with 30 days of history)."
            )
        elif not reach_now.complete:
            covered = date_range.days - len(reach_now.missing_days)
            unavailable["impressions"] = (
                f"Impressions data covers {covered} of {date_range.days} days of this period so far "
                "(YouTube publishes daily reports ~2 days late, and history starts 30 days before reports "
                "were enabled)."
            )
            reach_now = None
        else:
            reach_before = _section(
                unavailable, "impressions.previous", lambda: load_reach(ctx, previous_range)
            )
            if reach_before is not None and not reach_before.complete:
                reach_before = None
            video_types = (
                _section(unavailable, "impressions.by_content_type", lambda: classify_videos(ctx, date_range))
                or {}
            )

    def reach_for(data: ReachData | None, api_type: str) -> tuple[int | None, float | None]:
        if data is None or not video_types:
            return None, None
        ids = {vid for vid, kind in video_types.items() if kind == api_type}
        return data.totals(ids)

    by_type: dict[str, dict[str, Any]] = {}
    for key, api_type in CONTENT_TYPES.items():
        now = (current_ct or {}).get(key) or {}
        before = (previous_ct or {}).get(key) or {}
        impressions_now, ctr_now = reach_for(reach_now, api_type)
        impressions_before, ctr_before = reach_for(reach_before, api_type)
        entry = {
            name: _metric(now.get(name), before.get(name))
            for name in (
                "views",
                "engaged_views",
                "likes",
                "net_subscribers",
                "watch_time_hours",
                "average_view_duration_s",
            )
        }
        entry["impressions"] = _metric(impressions_now, impressions_before)
        entry["impressions_ctr_pct"] = _metric(ctr_now, ctr_before)
        by_type[key] = entry

    funnel: dict[str, Any] = {
        "impressions": None,
        "impressions_ctr_pct": None,
        "views_from_impressions": None,
        "average_view_duration_s": None,
        "watch_hours_from_impressions": None,
    }
    if reach_now is not None:
        impressions, ctr = reach_now.totals()
        funnel.update(
            {
                "impressions": impressions,
                "impressions_ctr_pct": ctr,
                "views_from_impressions": round(impressions * ctr / 100) if ctr is not None else None,
            }
        )
    unavailable["impressions_funnel.watch_metrics"] = (
        "Average view duration and watch time from impressions are Studio-only (no API source)."
    )

    # -- monthly trend ------------------------------------------------------------------------
    trend_start = max(add_months(date_range.end.replace(day=1), -(trend_months - 1)), ctx.channel_start())
    trend_range = DateRange(min(trend_start, date_range.end), date_range.end, f"last_{trend_months}_months")

    def build_monthly() -> list[dict[str, Any]]:
        rows = timeseries(
            ctx,
            trend_range,
            ["views", "estimatedMinutesWatched", "subscribersGained", "subscribersLost"],
            "month",
        )
        nets = [int(r["subscribersGained"] or 0) - int(r["subscribersLost"] or 0) for r in rows]
        running = (lifetime_net - sum(nets)) if lifetime_net is not None else None
        out = []
        for row, net in zip(rows, nets, strict=True):
            if running is not None:
                running += net
            out.append(
                {
                    "period": row["period"],
                    "views": row["views"],
                    "watch_hours": _hours(row["estimatedMinutesWatched"]),
                    "subscribers_gained": row["subscribersGained"],
                    "subscribers_lost": row["subscribersLost"],
                    "net_subscribers": net,
                    "total_subscribers": running,
                    "partial": row.get("partial", False),
                }
            )
        return out

    monthly = _section(unavailable, "monthly_trend", build_monthly) or []

    # -- audience -----------------------------------------------------------------------------
    def audience_rows(name: str, limit: int = 50) -> list[dict[str, Any]]:
        return _rows(
            _section(unavailable, f"audience.{name}", lambda: breakdown(ctx, name, date_range, limit=limit))
        )

    def complete_buckets(rows: list[dict[str, Any]], dim: str, codes: list[str]) -> list[dict[str, Any]]:
        values = {str(r.get(dim)): float(r.get("viewerPercentage") or 0) for r in rows}
        return [
            {"code": code, "label": label_for(dim, code), "viewer_pct": round(values.get(code, 0.0), 2)}
            for code in codes
        ]

    age_rows = audience_rows("age")
    gender_rows = audience_rows("gender")
    age = complete_buckets(age_rows, "ageGroup", AGE_CODES) if age_rows else []
    gender = complete_buckets(gender_rows, "gender", GENDER_CODES) if gender_rows else []
    if not age_rows and "audience.age" not in unavailable:
        unavailable["audience.age"] = (
            "YouTube returned no age data for this period (too few logged-in viewers)."
        )
    if not gender_rows and "audience.gender" not in unavailable:
        unavailable["audience.gender"] = "YouTube returned no gender data for this period."

    def shares(name: str, dim: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = audience_rows(name, limit)
        return [
            {
                "code": r.get(dim),
                "label": label_for(dim, r.get(dim)),
                "views": r.get("views"),
                "share_pct": r.get("share_pct"),
            }
            for r in rows
            if float(r.get("views") or 0) > 0
        ]

    countries = [
        {"code": r.get("country"), "views": r.get("views"), "share_pct": r.get("share_pct")}
        for r in audience_rows("country", top_countries)
    ]
    devices = shares("device", "deviceType")
    subscribed = shares("subscribed_status", "subscribedStatus")
    content_type = shares("content_type", "creatorContentType")

    # -- uploads, top videos, traffic ---------------------------------------------------------
    uploads = (
        _section(
            unavailable,
            "recent_uploads",
            lambda: recent_uploads(
                ctx, date_range.end, per_format=recent_uploads_per_format, min_age_days=min_upload_age_days
            ),
        )
        or {}
    )

    def build_top() -> list[dict[str, Any]]:
        rows = top_videos(ctx, date_range, limit=top_videos_limit)
        details = {
            item["id"]: summarize_video(item)
            for item in get_videos(ctx.api, [str(r["video_id"]) for r in rows], parts="snippet")
        }
        return [
            {
                "rank": r["rank"],
                "video_id": r["video_id"],
                "title": r.get("title"),
                "url": f"https://www.youtube.com/watch?v={r['video_id']}",
                "thumbnail_url": (details.get(str(r["video_id"])) or {}).get("thumbnail_url"),
                "published_at": r.get("published_at"),
                "content_type": r.get("content_type"),
                "views": r.get("views"),
                "watch_time_hours": r.get("watchTimeHours"),
                "average_view_duration_s": r.get("averageViewDuration"),
                "likes": r.get("likes"),
                "comments": r.get("comments"),
                "engagement_rate_pct": r.get("engagementRatePct"),
            }
            for r in rows
        ]

    top = _section(unavailable, "top_videos", build_top) or []
    traffic = [
        {
            "code": r.get("insightTrafficSourceType"),
            "label": r.get("label"),
            "views": r.get("views"),
            "share_pct": r.get("share_pct"),
        }
        for r in _rows(
            _section(unavailable, "traffic_sources", lambda: traffic_sources(ctx, date_range, limit=8))
        )
    ]

    # -- highlights ---------------------------------------------------------------------------
    def value(key: str) -> Any:
        return performance[key]["value"]

    gender_map = {g["code"]: g["viewer_pct"] for g in gender}
    full_months = [row for row in monthly if not row["partial"]] or monthly
    growth = None
    if len(full_months) >= 2 and full_months[0]["total_subscribers"] is not None:
        start_total = full_months[0]["total_subscribers"] - full_months[0]["net_subscribers"]
        if start_total > 0:
            growth = round((full_months[-1]["total_subscribers"] - start_total) / start_total * 100, 1)
    highlights = {
        "subscribers": subscribers,
        "subscribers_from_analytics": lifetime_net,
        "total_views": channel_info["total_views"],
        "video_count": channel_info["video_count"],
        "period_views": value("views"),
        "period_watch_time_hours": value("watch_time_hours"),
        "period_net_subscribers": value("net_subscribers"),
        "engagement_rate_pct": value("engagement_rate_pct"),
        "average_view_duration_s": value("average_view_duration_s"),
        "avg_views_per_video_recent": ((uploads.get("videos") or {}).get("avg_views")),
        "avg_views_per_short_recent": ((uploads.get("shorts") or {}).get("avg_views")),
        "top_country": countries[0] if countries else None,
        "top_age_group": max(age, key=lambda a: a["viewer_pct"], default=None),
        "female_pct": gender_map.get("female"),
        "male_pct": gender_map.get("male"),
        "mobile_share_pct": next((d["share_pct"] for d in devices if d["code"] == "MOBILE"), None),
        "shorts_view_share_pct": next((c["share_pct"] for c in content_type if c["code"] == "SHORTS"), None),
        "subscriber_growth_pct": growth,
        "subscriber_growth_window": f"{full_months[0]['period']}..{full_months[-1]['period']}"
        if growth is not None
        else None,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_through": date_range.end.isoformat(),
        "period": date_range.to_dict(),
        "comparison_period": previous_range.to_dict(),
        "channel": channel_info,
        "highlights": highlights,
        "performance": performance,
        "performance_by_content_type": by_type,
        "impressions_funnel": funnel,
        "daily_trend": trend_daily,
        "monthly_trend": monthly,
        "audience": {
            "age": age,
            "gender": gender,
            "countries": countries,
            "devices": devices,
            "subscribed_status": subscribed,
            "content_type": content_type,
        },
        "recent_uploads": uploads,
        "top_videos": top,
        "traffic_sources": traffic,
        "unavailable": unavailable,
        "notes": [
            "Dates are US Pacific time. Analytics lag 2-3 days, so the period ends at data_through, a few "
            "days before generated_at; Studio's live numbers will be slightly higher.",
            "channel.subscribers is YouTube's public count, rounded down to 3 significant figures above "
            "1,000. subscribers_from_analytics = lifetime gained - lost through data_through (exact, but "
            "can differ slightly from Studio's live count).",
            "All *_pct values are 0-100; *_s values are seconds. Audience age/gender are shares of "
            "logged-in viewers. engagement_rate_pct = (likes + comments + shares) / views.",
            "Traffic source codes are YouTube's insightTrafficSourceType values (SUBSCRIBER = Browse "
            "features).",
        ],
    }


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(title="Media kit data", annotations=READ_ONLY)
    @tool_errors
    def get_media_kit_data(
        period: PeriodArg = "last_28_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        trend_months: Annotated[int, Field(ge=3, le=36, description="Months in monthly_trend.")] = 12,
        recent_uploads: Annotated[
            int,
            Field(ge=3, le=50, description="Recent long-form videos and Shorts to list and average (each)."),
        ] = 10,
        min_upload_age_days: Annotated[
            int,
            Field(ge=0, le=90, description="Skip uploads younger than this (their views are still growing)."),
        ] = 7,
        top_countries: Annotated[int, Field(ge=1, le=50, description="Countries to include.")] = 8,
        include_revenue: Annotated[
            bool, Field(description="Add revenue/RPM to performance (most media kits don't show it).")
        ] = False,
        save_to: Annotated[
            str | None,
            Field(description="Also save the snapshot as JSON (bare filename -> output directory)."),
        ] = None,
    ) -> dict[str, Any]:
        """Everything a creator media kit / sponsor deck needs, as one stable JSON snapshot
        (schema_version 1, snake_case, *_pct 0-100, *_s seconds): channel profile, period
        performance vs previous period, Shorts vs long-form performance, impressions funnel,
        daily and monthly trends, audience age/gender/countries/devices, recent uploads with
        thumbnails, top videos and traffic sources. Anything YouTube can't provide is null and
        explained in `unavailable` instead of failing the call."""
        date_range = ctx.resolve(period, start_date, end_date, default="last_28_days")
        result = build_media_kit(
            ctx,
            date_range,
            trend_months=trend_months,
            recent_uploads_per_format=recent_uploads,
            min_upload_age_days=min_upload_age_days,
            top_countries=top_countries,
            include_revenue=include_revenue,
        )
        if save_to:
            result["saved_to"] = ctx.save_json(result, save_to, default_stem="media-kit")
        return result
