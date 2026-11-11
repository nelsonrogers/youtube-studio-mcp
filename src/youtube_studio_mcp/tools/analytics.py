"""YouTube Analytics tools: summaries, trends, rankings, audience, traffic, retention, revenue."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from youtube_studio_mcp.analytics import (
    ADDITIVE_METRICS,
    WEIGHTED_METRICS,
    Granularity,
    Report,
    add_shares,
    aggregate_daily_rows,
    join_filters,
    label_for,
    pct_change,
    run_report,
    run_report_with_optional_metrics,
    video_filter,
    weight_metrics_needed,
)
from youtube_studio_mcp.auth import ANALYTICS_MONETARY_READONLY, AUTH_COMMAND
from youtube_studio_mcp.context import (
    READ_ONLY,
    AppContext,
    EndDateArg,
    PeriodArg,
    SaveToArg,
    StartDateArg,
    VideoIdArg,
    require_scope,
    tool_errors,
)
from youtube_studio_mcp.periods import DateRange, PeriodError
from youtube_studio_mcp.youtube import YouTubeAPIError, get_videos, parse_duration, summarize_video, to_int

ContentType = Literal["shorts", "videos", "live"]
CONTENT_TYPE_VALUES = {"shorts": "SHORTS", "videos": "VIDEO_ON_DEMAND", "live": "LIVE_STREAM"}

SUMMARY_METRICS = (
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
    "subscribersLost",
    "likes",
    "dislikes",
    "comments",
    "shares",
)
REVENUE_SUMMARY_METRICS = (
    "estimatedRevenue",
    "estimatedAdRevenue",
    "grossRevenue",
    "cpm",
    "playbackBasedCpm",
    "monetizedPlaybacks",
    "adImpressions",
)
TOP_VIDEO_SORTS = (
    "views",
    "estimatedMinutesWatched",
    "subscribersGained",
    "subscribersLost",
    "estimatedRevenue",
)
TOP_VIDEO_METRICS = (
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "likes",
    "comments",
    "shares",
    "subscribersGained",
)
DEFAULT_TIMESERIES_METRICS = ("views", "estimatedMinutesWatched", "subscribersGained", "subscribersLost")


@dataclass(frozen=True)
class Breakdown:
    dimensions: tuple[str, ...]
    metrics: tuple[str, ...]
    filters: str | None = None
    rank_metric: str | None = "views"
    description: str = ""


BREAKDOWNS: dict[str, Breakdown] = {
    "age": Breakdown(("ageGroup",), ("viewerPercentage",), rank_metric=None),
    "gender": Breakdown(("gender",), ("viewerPercentage",), rank_metric=None),
    "age_gender": Breakdown(("ageGroup", "gender"), ("viewerPercentage",), rank_metric=None),
    "country": Breakdown(
        ("country",), ("views", "estimatedMinutesWatched", "averageViewDuration", "subscribersGained")
    ),
    "us_state": Breakdown(
        ("province",), ("views", "estimatedMinutesWatched", "averageViewDuration"), filters="country==US"
    ),
    "device": Breakdown(("deviceType",), ("views", "estimatedMinutesWatched")),
    "operating_system": Breakdown(("operatingSystem",), ("views", "estimatedMinutesWatched")),
    "subscribed_status": Breakdown(
        ("subscribedStatus",), ("views", "estimatedMinutesWatched", "averageViewDuration", "likes", "shares")
    ),
    "content_type": Breakdown(
        ("creatorContentType",),
        (
            "views",
            "estimatedMinutesWatched",
            "averageViewDuration",
            "averageViewPercentage",
            "likes",
            "shares",
        ),
    ),
    "playback_location": Breakdown(("insightPlaybackLocationType",), ("views", "estimatedMinutesWatched")),
    "sharing_service": Breakdown(("sharingService",), ("shares",), rank_metric="shares"),
}
BreakdownName = Literal[
    "age",
    "gender",
    "age_gender",
    "country",
    "us_state",
    "device",
    "operating_system",
    "subscribed_status",
    "content_type",
    "playback_location",
    "sharing_service",
]

# ------------------------------------------------------------------------------- shared helpers


def totals(
    ctx: AppContext,
    date_range: DateRange,
    metrics: tuple[str, ...],
    *,
    filters: str | None = None,
    optional: tuple[str, ...] = (),
) -> dict[str, Any]:
    report = run_report_with_optional_metrics(ctx.api, date_range, metrics, optional, filters=filters)
    return dict(report.rows[0]) if report.rows else dict.fromkeys(report.columns or metrics, 0)


def derive(values: dict[str, Any]) -> dict[str, Any]:
    """Add commonly wanted derived metrics."""
    out = dict(values)
    views = float(out.get("views") or 0)
    if "estimatedMinutesWatched" in out:
        out["watchTimeHours"] = round(float(out["estimatedMinutesWatched"] or 0) / 60, 1)
    if "subscribersGained" in out and "subscribersLost" in out:
        out["netSubscribers"] = int(out["subscribersGained"] or 0) - int(out["subscribersLost"] or 0)
    if views and {"likes", "comments", "shares"} <= out.keys():
        interactions = sum(float(out[k] or 0) for k in ("likes", "comments", "shares"))
        out["engagementRatePct"] = round(interactions / views * 100, 2)
    if views and out.get("estimatedRevenue") is not None:
        out["rpm"] = round(float(out["estimatedRevenue"]) / views * 1000, 2)
    return out


def performance_summary(
    ctx: AppContext,
    date_range: DateRange,
    *,
    video_id: str | None = None,
    compare: bool = True,
    include_revenue: bool = False,
) -> dict[str, Any]:
    filters = video_filter([video_id]) if video_id else None
    current = derive(totals(ctx, date_range, SUMMARY_METRICS, filters=filters, optional=("engagedViews",)))
    previous: dict[str, Any] | None = None
    comparison_range = date_range.previous() if compare else None
    if comparison_range is not None:
        previous = derive(
            totals(ctx, comparison_range, SUMMARY_METRICS, filters=filters, optional=("engagedViews",))
        )
    notes: list[str] = []
    revenue_unavailable: str | None = None
    if include_revenue:
        try:
            revenue = totals(ctx, date_range, REVENUE_SUMMARY_METRICS, filters=filters)
            current = derive({**current, **revenue})
            if comparison_range is not None and previous is not None:
                previous = derive(
                    {**previous, **totals(ctx, comparison_range, REVENUE_SUMMARY_METRICS, filters=filters)}
                )
        except YouTubeAPIError as exc:
            if exc.status not in (401, 403):
                raise
            revenue_unavailable = (
                "Revenue metrics unavailable: the channel must be in the YouTube Partner Program and "
                f"revenue access must be granted (`{AUTH_COMMAND}`)."
            )
    metrics: dict[str, Any] = {}
    for name, value in current.items():
        entry: dict[str, Any] = {"value": value}
        if previous is not None:
            entry["previous"] = previous.get(name)
            entry["change_pct"] = pct_change(value, previous.get(name))
        metrics[name] = entry
    result: dict[str, Any] = {"date_range": date_range.to_dict(), "metrics": metrics}
    if comparison_range is not None:
        result["comparison_range"] = comparison_range.to_dict()
    if video_id:
        result["video_id"] = video_id
    if revenue_unavailable:
        result["revenue_unavailable"] = revenue_unavailable
    notes.append(
        "averageViewDuration is in seconds; averageViewPercentage and engagementRatePct are 0-100; "
        "engagementRatePct = (likes + comments + shares) / views."
    )
    result["notes"] = notes
    return result


def timeseries(
    ctx: AppContext,
    date_range: DateRange,
    metrics: list[str],
    granularity: Granularity,
    *,
    video_id: str | None = None,
    content_type: str | None = None,
) -> list[dict[str, Any]]:
    query_metrics = list(dict.fromkeys([*metrics, *weight_metrics_needed(metrics)]))
    dimensions = ["day", "creatorContentType"] if content_type else ["day"]
    report = run_report(
        ctx.api,
        date_range,
        query_metrics,
        dimensions=dimensions,
        filters=video_filter([video_id]) if video_id else None,
        sort="day",
    )
    rows = report.rows
    if content_type:
        wanted = CONTENT_TYPE_VALUES[content_type]
        rows = [row for row in rows if row.get("creatorContentType") == wanted]
    out = aggregate_daily_rows(rows, query_metrics, granularity, date_range=date_range)
    extra = set(query_metrics) - set(metrics)
    for row in out:
        for name in extra:
            row.pop(name, None)
    return out


def subscriber_baseline(ctx: AppContext, date_range: DateRange) -> int:
    """Net subscribers from channel creation up to the day before ``date_range`` starts."""
    start = ctx.channel_start()
    end = date_range.start - timedelta(days=1)
    if end < start:
        return 0
    values = totals(ctx, DateRange(start, end, "baseline"), ("subscribersGained", "subscribersLost"))
    return int(values.get("subscribersGained") or 0) - int(values.get("subscribersLost") or 0)


def video_titles(ctx: AppContext, video_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not video_ids:
        return {}
    return {
        item["id"]: summarize_video(item)
        for item in get_videos(ctx.api, video_ids, parts="snippet,contentDetails,statistics,status")
    }


def top_videos(
    ctx: AppContext,
    date_range: DateRange,
    *,
    metric: str = "views",
    limit: int = 10,
    content_type: str | None = None,
    include_details: bool = True,
) -> list[dict[str, Any]]:
    metrics = list(TOP_VIDEO_METRICS)
    if metric not in metrics:
        metrics.append(metric)
    fetch = 200 if content_type else limit
    try:
        report = run_report_with_optional_metrics(
            ctx.api,
            date_range,
            metrics,
            ("engagedViews",),
            dimensions=["video", "creatorContentType"],
            sort=f"-{metric}",
            max_results=fetch,
        )
    except YouTubeAPIError as exc:
        if exc.status != 400 or content_type:
            raise
        report = run_report(
            ctx.api, date_range, metrics, dimensions=["video"], sort=f"-{metric}", max_results=fetch
        )
    rows = report.rows
    if content_type:
        wanted = CONTENT_TYPE_VALUES[content_type]
        rows = [row for row in rows if row.get("creatorContentType") == wanted]
    rows = rows[:limit]
    details = video_titles(ctx, [str(row["video"]) for row in rows]) if include_details else {}
    out = []
    for rank, row in enumerate(rows, start=1):
        video_id = str(row.pop("video"))
        info = details.get(video_id, {})
        content = row.pop("creatorContentType", None)
        entry: dict[str, Any] = {
            "rank": rank,
            "video_id": video_id,
            "title": info.get("title"),
            "published_at": info.get("published_at"),
            "content_type": label_for("creatorContentType", content) if content else None,
            "duration_seconds": info.get("duration_seconds"),
        }
        entry.update(derive(row))
        out.append(entry)
    return out


def breakdown(
    ctx: AppContext,
    name: str,
    date_range: DateRange,
    *,
    video_id: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    spec = BREAKDOWNS[name]
    filters = join_filters(spec.filters, video_filter([video_id]) if video_id else None)
    report = run_report(ctx.api, date_range, spec.metrics, dimensions=spec.dimensions, filters=filters)
    rows = [dict(row) for row in report.rows]
    for row in rows:
        for dim in spec.dimensions:
            label = label_for(dim, row.get(dim))
            if label:
                row[f"{dim}Label"] = label
        if "averageViewDuration" in row:
            row["averageViewDuration"] = round(float(row["averageViewDuration"] or 0), 1)
    notes: list[str] = []
    if spec.rank_metric:
        rows.sort(key=lambda r: float(r.get(spec.rank_metric or "") or 0), reverse=True)
        add_shares(rows, spec.rank_metric)
        notes.append(f"share_pct = share of {spec.rank_metric} in this breakdown.")
    total_rows = len(rows)
    result: dict[str, Any] = {"breakdown": name, "date_range": date_range.to_dict()}
    if name == "age_gender":
        by_age: dict[str, float] = {}
        by_gender: dict[str, float] = {}
        for row in rows:
            by_age[str(row["ageGroupLabel"])] = by_age.get(str(row["ageGroupLabel"]), 0) + float(
                row["viewerPercentage"]
            )
            by_gender[str(row["genderLabel"])] = by_gender.get(str(row["genderLabel"]), 0) + float(
                row["viewerPercentage"]
            )
        result["by_age"] = {k: round(v, 2) for k, v in by_age.items()}
        result["by_gender"] = {k: round(v, 2) for k, v in by_gender.items()}
    if name in {"age", "gender", "age_gender"}:
        notes.append("viewerPercentage is the share of logged-in viewers (0-100).")
        rows.sort(key=lambda r: (str(r.get("ageGroup", "")), str(r.get("gender", ""))))
    if not rows:
        notes.append(
            "No rows returned. YouTube withholds breakdowns when there is too little data for the period "
            "(privacy thresholds); try a longer period."
        )
    result["rows"] = rows[:limit]
    if total_rows > limit:
        result["truncated_from"] = total_rows
    result["notes"] = notes
    return result


def traffic_sources(
    ctx: AppContext,
    date_range: DateRange,
    *,
    video_id: str | None = None,
    source_type: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    vf = video_filter([video_id]) if video_id else None
    if not source_type:
        report = run_report(
            ctx.api,
            date_range,
            ["views", "estimatedMinutesWatched"],
            dimensions=["insightTrafficSourceType"],
            filters=vf,
        )
        rows = [dict(row) for row in report.rows]
        for row in rows:
            row["label"] = label_for("insightTrafficSourceType", row["insightTrafficSourceType"])
        rows.sort(key=lambda r: float(r.get("views") or 0), reverse=True)
        add_shares(rows, "views")
        return {
            "date_range": date_range.to_dict(),
            "rows": rows[:limit],
            "notes": [
                "Call again with source_type (e.g. 'YT_SEARCH' for search terms, 'RELATED_VIDEO' for the "
                "videos you were suggested next to, 'EXT_URL' for websites) to drill down."
            ],
        }
    source = source_type.strip().upper()
    report = run_report(
        ctx.api,
        date_range,
        ["views", "estimatedMinutesWatched"],
        dimensions=["insightTrafficSourceDetail"],
        filters=join_filters(f"insightTrafficSourceType=={source}", vf),
        sort="-views",
        max_results=min(limit, 25),
    )
    rows = [dict(row) for row in report.rows]
    add_shares(rows, "views")
    if source in {"RELATED_VIDEO", "END_SCREEN"} and rows:
        titles = video_titles(ctx, [str(row["insightTrafficSourceDetail"]) for row in rows])
        for row in rows:
            info = titles.get(str(row["insightTrafficSourceDetail"]))
            if info:
                row["video_title"] = info.get("title")
    return {
        "date_range": date_range.to_dict(),
        "source_type": source,
        "source_label": label_for("insightTrafficSourceType", source),
        "rows": rows,
        "notes": [
            "Detail rows are YouTube's top 25 at most; items with very few views are omitted.",
            "share_pct is relative to the rows returned, not to all views from this source.",
        ],
    }


def _timestamp(seconds: float) -> str:
    total = round(seconds)
    return (
        f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
        if total >= 3600
        else f"{total // 60}:{total % 60:02d}"
    )


# ------------------------------------------------------------------------------- tools


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(title="Performance summary", annotations=READ_ONLY)
    @tool_errors
    def get_performance_summary(
        period: PeriodArg = "last_28_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        video_id: VideoIdArg = None,
        compare_to_previous: Annotated[
            bool, Field(description="Also fetch the previous period of equal length and compute % change.")
        ] = True,
        include_revenue: Annotated[
            bool, Field(description="Add estimated revenue, RPM, CPM etc. (monetized channels only).")
        ] = False,
    ) -> dict[str, Any]:
        """Headline numbers for a period, like Studio's Overview tab: views, engaged views, watch
        time (hours), average view duration/percentage, subscribers gained/lost/net, likes,
        comments, shares, engagement rate, optionally revenue, each with the previous period and
        % change. Works for the channel or a single video."""
        date_range = ctx.resolve(period, start_date, end_date)
        if include_revenue:
            require_scope(ctx, ANALYTICS_MONETARY_READONLY, purpose="Revenue data", command=AUTH_COMMAND)
        return performance_summary(
            ctx, date_range, video_id=video_id, compare=compare_to_previous, include_revenue=include_revenue
        )

    @mcp.tool(title="Metrics over time", annotations=READ_ONLY)
    @tool_errors
    def get_metrics_over_time(
        metrics: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Analytics metric names. Default: views, estimatedMinutesWatched, subscribersGained, "
                    "subscribersLost. Others: engagedViews, likes, comments, shares, averageViewDuration, "
                    "averageViewPercentage, estimatedRevenue, estimatedAdRevenue, cpm, playbackBasedCpm, ..."
                )
            ),
        ] = None,
        granularity: Annotated[Granularity, Field(description="Bucket size. Weeks start on Monday.")] = "day",
        period: PeriodArg = "last_90_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        video_id: VideoIdArg = None,
        content_type: Annotated[
            ContentType | None, Field(description="Only Shorts, regular videos, or live streams.")
        ] = None,
        include_cumulative_subscribers: Annotated[
            bool,
            Field(
                description=(
                    "Add totalSubscribers per bucket (running total of net subscribers since the channel "
                    "started; great for growth charts). Channel-wide only."
                )
            ),
        ] = False,
        save_to: SaveToArg = None,
    ) -> dict[str, Any]:
        """A time series of metrics by day, week, month or year — for trend analysis and growth
        charts. Adds netSubscribers when gained and lost are both requested. Buckets cut off by
        the date range are flagged partial=true."""
        date_range = ctx.resolve(period, start_date, end_date, default="last_90_days")
        wanted = list(dict.fromkeys(metrics or DEFAULT_TIMESERIES_METRICS))
        unknown = [m for m in wanted if m not in ADDITIVE_METRICS and m not in WEIGHTED_METRICS]
        if unknown and granularity != "day":
            raise ToolError(
                f"Metrics {unknown} can only be fetched by day here; use query_analytics for anything else."
            )
        if include_cumulative_subscribers:
            if video_id or content_type:
                raise ToolError("include_cumulative_subscribers only works for the whole channel.")
            for name in ("subscribersGained", "subscribersLost"):
                if name not in wanted:
                    wanted.append(name)
        if any(
            m in wanted
            for m in ("estimatedRevenue", "estimatedAdRevenue", "grossRevenue", "cpm", "playbackBasedCpm")
        ):
            require_scope(ctx, ANALYTICS_MONETARY_READONLY, purpose="Revenue data", command=AUTH_COMMAND)
        rows = timeseries(ctx, date_range, wanted, granularity, video_id=video_id, content_type=content_type)
        if "subscribersGained" in wanted and "subscribersLost" in wanted:
            for row in rows:
                row["netSubscribers"] = int(row.get("subscribersGained") or 0) - int(
                    row.get("subscribersLost") or 0
                )
        notes = ["averageViewDuration is in seconds; estimatedMinutesWatched is minutes."]
        if include_cumulative_subscribers:
            running = subscriber_baseline(ctx, date_range)
            for row in rows:
                running += int(row["netSubscribers"])
                row["totalSubscribers"] = running
            notes.append(
                "totalSubscribers is derived from analytics (net gains since channel creation); it can "
                "differ slightly from the public count, which YouTube rounds."
            )
        result: dict[str, Any] = {
            "date_range": date_range.to_dict(),
            "granularity": granularity,
            "rows": rows,
            "notes": notes,
        }
        if save_to:
            result["saved_to"] = ctx.save_rows(rows, save_to, default_stem="metrics")
        return result

    @mcp.tool(title="Top videos", annotations=READ_ONLY)
    @tool_errors
    def get_top_videos(
        metric: Annotated[
            Literal[
                "views", "estimatedMinutesWatched", "subscribersGained", "subscribersLost", "estimatedRevenue"
            ],
            Field(description="What to rank by."),
        ] = "views",
        limit: Annotated[int, Field(ge=1, le=200, description="How many videos.")] = 10,
        period: PeriodArg = "last_28_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        content_type: Annotated[
            ContentType | None, Field(description="Only rank Shorts, regular videos, or live streams.")
        ] = None,
        save_to: SaveToArg = None,
    ) -> dict[str, Any]:
        """Rank the channel's videos by performance within a period (like Studio's 'Top content'),
        with title, publish date, YouTube's content type (Shorts/Videos/Live), views, watch time,
        average view duration/percentage, likes, comments, shares, subscribers gained and
        engagement rate."""
        date_range = ctx.resolve(period, start_date, end_date)
        if metric == "estimatedRevenue":
            require_scope(ctx, ANALYTICS_MONETARY_READONLY, purpose="Revenue ranking", command=AUTH_COMMAND)
        rows = top_videos(ctx, date_range, metric=metric, limit=limit, content_type=content_type)
        result: dict[str, Any] = {"date_range": date_range.to_dict(), "ranked_by": metric, "videos": rows}
        if save_to:
            result["saved_to"] = ctx.save_rows(rows, save_to, default_stem="top-videos")
        return result

    @mcp.tool(title="Audience breakdown", annotations=READ_ONLY)
    @tool_errors
    def get_audience_breakdown(
        dimension: Annotated[
            BreakdownName,
            Field(
                description=(
                    "age / gender / age_gender (viewer %), country, us_state, device (phone, TV, ...), "
                    "operating_system, subscribed_status (subscribers vs not), content_type "
                    "(Shorts vs videos vs live), playback_location, sharing_service (where shares go)."
                )
            ),
        ],
        period: PeriodArg = "last_90_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        video_id: VideoIdArg = None,
        limit: Annotated[int, Field(ge=1, le=250, description="Maximum rows (largest first).")] = 25,
        save_to: SaveToArg = None,
    ) -> dict[str, Any]:
        """Who watches and how: demographics, geography, devices, subscriber vs non-subscriber
        views, Shorts vs long-form split, playback locations. Rows include readable labels and
        share_pct of views, ready for media-kit charts."""
        date_range = ctx.resolve(period, start_date, end_date, default="last_90_days")
        result = breakdown(ctx, dimension, date_range, video_id=video_id, limit=limit)
        if save_to:
            result["saved_to"] = ctx.save_rows(result["rows"], save_to, default_stem=f"audience-{dimension}")
        return result

    @mcp.tool(title="Traffic sources", annotations=READ_ONLY)
    @tool_errors
    def get_traffic_sources(
        period: PeriodArg = "last_28_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        video_id: VideoIdArg = None,
        source_type: Annotated[
            str | None,
            Field(
                description=(
                    "Drill into one source: YT_SEARCH (search terms), RELATED_VIDEO (suggested-video "
                    "placements), EXT_URL (websites/apps), YT_CHANNEL, SUBSCRIBER, HASHTAGS, "
                    "SOUND_PAGE, ADVERTISING, YT_OTHER_PAGE. Omit for the overview."
                )
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=25, description="Maximum rows.")] = 25,
    ) -> dict[str, Any]:
        """How viewers find the content: views and watch time by traffic source (search, suggested,
        browse, Shorts feed, external, ...) or, with source_type, the top search terms / referring
        videos / websites for that source."""
        date_range = ctx.resolve(period, start_date, end_date)
        return traffic_sources(ctx, date_range, video_id=video_id, source_type=source_type, limit=limit)

    @mcp.tool(title="Audience retention", annotations=READ_ONLY)
    @tool_errors
    def get_audience_retention(
        video_id: Annotated[str, Field(description="The video to analyse (one ID).")],
        period: PeriodArg = None,
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        audience_type: Annotated[
            Literal["ORGANIC", "AD_INSTREAM", "AD_INDISPLAY"] | None,
            Field(description="Only organic views, or views from in-stream / in-feed ads."),
        ] = None,
        subscribed_status: Annotated[
            Literal["SUBSCRIBED", "UNSUBSCRIBED"] | None,
            Field(description="Only subscribers or non-subscribers."),
        ] = None,
        points: Annotated[
            int,
            Field(
                ge=10, le=100, description="Number of points in the returned curve (down-sampled from 100)."
            ),
        ] = 50,
    ) -> dict[str, Any]:
        """The audience-retention curve for one video, with timestamps, key retention milestones
        and the steepest drop-off moments. Defaults to the video's lifetime.

        audience_watch_ratio = share of viewers still watching at that moment (can exceed 1 when
        people rewatch a part). relative_retention compares with similar-length YouTube videos
        (0-1; above 0.5 is better than average)."""
        items = get_videos(ctx.api, [video_id], parts="snippet,contentDetails")
        if not items:
            raise ToolError(f"Video {video_id} was not found on this channel.")
        info = summarize_video(items[0])
        duration = parse_duration((items[0].get("contentDetails") or {}).get("duration")) or 0
        published = str(info.get("published_at") or "")[:10]
        try:
            if period or start_date:
                date_range = ctx.resolve(period, start_date, end_date)
            else:
                date_range = ctx.resolve(None, published or None, end_date, default="all_time")
        except PeriodError as exc:
            raise ToolError(
                f"No analytics yet for this range ({exc}). Videos published in the last ~3 days have no "
                "retention data yet."
            ) from exc
        filters = join_filters(
            f"video=={video_id}",
            f"audienceType=={audience_type}" if audience_type else None,
            f"subscribedStatus=={subscribed_status}" if subscribed_status else None,
        )
        report = run_report(
            ctx.api,
            date_range,
            ["audienceWatchRatio", "relativeRetentionPerformance"],
            dimensions=["elapsedVideoTimeRatio"],
            filters=filters,
        )
        report.rows.sort(key=lambda row: float(row["elapsedVideoTimeRatio"]))
        curve: list[dict[str, Any]] = [
            {
                "elapsed_pct": round(float(row["elapsedVideoTimeRatio"]) * 100, 1),
                "timestamp": _timestamp(float(row["elapsedVideoTimeRatio"]) * duration),
                "audience_watch_ratio": round(float(row["audienceWatchRatio"]), 4),
                "relative_retention": round(float(row["relativeRetentionPerformance"]), 4),
            }
            for row in report.rows
        ]
        summary: dict[str, Any] = {}
        if curve:

            def at(pct: float) -> float | None:
                closest = min(curve, key=lambda p: abs(p["elapsed_pct"] - pct))
                return float(closest["audience_watch_ratio"])

            summary = {
                "retention_at_25pct": at(25),
                "retention_at_50pct": at(50),
                "retention_at_75pct": at(75),
                "retention_at_end": float(curve[-1]["audience_watch_ratio"]),
                "average_relative_retention": round(
                    sum(float(p["relative_retention"]) for p in curve) / len(curve), 3
                ),
            }
            drops = []
            for before, after in itertools.pairwise(curve):
                drop = float(before["audience_watch_ratio"]) - float(after["audience_watch_ratio"])
                drops.append((drop, after))
            drops.sort(key=lambda item: item[0], reverse=True)
            summary["steepest_drops"] = [
                {"at": point["timestamp"], "elapsed_pct": point["elapsed_pct"], "drop": round(drop, 4)}
                for drop, point in drops[:3]
                if drop > 0
            ]
            spikes = [p for p in curve[1:] if float(p["audience_watch_ratio"]) > 1]
            if spikes:
                summary["rewatched_moments"] = [p["timestamp"] for p in spikes[:5]]
        step = max(1, round(len(curve) / points))
        sampled = curve[::step]
        if curve and sampled[-1] is not curve[-1]:
            sampled.append(curve[-1])
        stats = totals(
            ctx,
            date_range,
            ("views", "averageViewDuration", "averageViewPercentage"),
            filters=f"video=={video_id}",
        )
        return {
            "video": {
                "video_id": video_id,
                "title": info.get("title"),
                "published_at": info.get("published_at"),
                "duration_seconds": duration,
                "duration": _timestamp(duration),
            },
            "date_range": date_range.to_dict(),
            "views": to_int(stats.get("views")),
            "average_view_duration_seconds": stats.get("averageViewDuration"),
            "average_view_percentage": stats.get("averageViewPercentage"),
            "summary": summary,
            "curve": sampled,
            "notes": [] if curve else ["No retention data yet (new or low-view videos may have none)."],
        }

    @mcp.tool(title="Revenue report", annotations=READ_ONLY)
    @tool_errors
    def get_revenue(
        period: PeriodArg = "last_90_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        granularity: Annotated[Granularity, Field(description="Bucket size for the trend.")] = "month",
        top_videos_limit: Annotated[
            int, Field(ge=0, le=50, description="Top-earning videos to include.")
        ] = 10,
        currency: Annotated[str | None, Field(description="ISO 4217 code, e.g. 'EUR'. Default USD.")] = None,
    ) -> dict[str, Any]:
        """Monetization report (YouTube Partner Program channels): estimated revenue, ad revenue,
        gross revenue, RPM, CPM, playback-based CPM, monetized playbacks and ad impressions, with a
        trend and the top-earning videos."""
        require_scope(ctx, ANALYTICS_MONETARY_READONLY, purpose="Revenue data", command=AUTH_COMMAND)
        date_range = ctx.resolve(period, start_date, end_date, default="last_90_days")
        try:
            total_report = run_report(
                ctx.api, date_range, [*REVENUE_SUMMARY_METRICS, "views"], currency=currency
            )
            total = derive(total_report.rows[0] if total_report.rows else {})
            daily = run_report(
                ctx.api,
                date_range,
                ["estimatedRevenue", "estimatedAdRevenue", "views", "cpm", "adImpressions"],
                dimensions=["day"],
                sort="day",
                currency=currency,
            )
            trend = aggregate_daily_rows(
                daily.rows,
                ["estimatedRevenue", "estimatedAdRevenue", "views", "cpm", "adImpressions"],
                granularity,
                date_range=date_range,
            )
            for row in trend:
                views = float(row.get("views") or 0)
                row["rpm"] = round(float(row["estimatedRevenue"] or 0) / views * 1000, 2) if views else None
                row.pop("adImpressions", None)
            top: list[dict[str, Any]] = []
            if top_videos_limit:
                ranked = run_report(
                    ctx.api,
                    date_range,
                    ["estimatedRevenue", "views"],
                    dimensions=["video"],
                    sort="-estimatedRevenue",
                    max_results=top_videos_limit,
                    currency=currency,
                )
                titles = video_titles(ctx, [str(row["video"]) for row in ranked.rows])
                for row in ranked.rows:
                    views = float(row.get("views") or 0)
                    top.append(
                        {
                            "video_id": row["video"],
                            "title": (titles.get(str(row["video"])) or {}).get("title"),
                            "estimatedRevenue": row["estimatedRevenue"],
                            "views": row["views"],
                            "rpm": round(float(row["estimatedRevenue"] or 0) / views * 1000, 2)
                            if views
                            else None,
                        }
                    )
        except YouTubeAPIError as exc:
            if exc.status == 403:
                raise ToolError(
                    "YouTube refused revenue data. The channel must be in the YouTube Partner Program and "
                    "the signed-in account must be its owner with revenue access granted "
                    f"(`{AUTH_COMMAND}`).\nDetails: {exc}"
                ) from exc
            raise
        return {
            "date_range": date_range.to_dict(),
            "currency": (currency or "USD").upper(),
            "totals": total,
            "trend": trend,
            "top_earning_videos": top,
            "notes": [
                "estimatedRevenue = net revenue to the creator from all Google-sold ads and other sources; "
                "rpm = estimatedRevenue per 1,000 views; cpm = gross ad revenue per 1,000 ad impressions.",
                "Revenue figures are estimates and are finalised by YouTube later in the month.",
            ],
        }

    @mcp.tool(title="Custom analytics query", annotations=READ_ONLY)
    @tool_errors
    def query_analytics(
        metrics: Annotated[
            list[str], Field(min_length=1, description="Metric API names, e.g. ['views', 'likes'].")
        ],
        dimensions: Annotated[
            list[str] | None,
            Field(
                description="Dimension API names, e.g. ['day'], ['video'], ['country', 'creatorContentType']."
            ),
        ] = None,
        filters: Annotated[
            str | None,
            Field(description="Analytics filter string, e.g. 'video==ID1,ID2;country==US' (';' = AND)."),
        ] = None,
        sort: Annotated[
            str | None, Field(description="Comma-separated sort, '-' for descending, e.g. '-views'.")
        ] = None,
        max_results: Annotated[int | None, Field(ge=1, le=10000, description="Row limit.")] = None,
        period: PeriodArg = "last_28_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        currency: Annotated[str | None, Field(description="Currency for revenue metrics (ISO 4217).")] = None,
        save_to: SaveToArg = None,
    ) -> dict[str, Any]:
        """Run any YouTube Analytics API v2 report for the channel (ids=channel==MINE) — the escape
        hatch when no other tool fits. See
        https://developers.google.com/youtube/analytics/channel_reports for valid combinations.
        Notes: the 'video' dimension needs sort and max_results <= 200; 'insightTrafficSourceDetail'
        needs a filter on insightTrafficSourceType, sort and max_results <= 25; demographics
        (ageGroup/gender) only support viewerPercentage; the 'month' dimension aligns dates to
        whole months automatically. YouTube's error message is returned if a combination is invalid."""
        date_range = ctx.resolve(period, start_date, end_date)
        notes: list[str] = []
        dims = list(dimensions or [])
        if "month" in dims:
            # Documented requirement: both dates must be the first day of a month.
            first_of_end = date_range.end.replace(day=1)
            date_range = DateRange(date_range.start.replace(day=1), first_of_end, date_range.label)
            notes.append(
                "Dates were aligned to the first day of each month, as the month dimension requires; "
                "the last month may be incomplete."
            )
        report: Report = run_report(
            ctx.api,
            date_range,
            metrics,
            dimensions=dims,
            filters=filters,
            sort=sort,
            max_results=max_results,
            currency=currency,
        )
        for row in report.rows:
            for dim in dims:
                label = label_for(dim, row.get(dim))
                if label:
                    row[f"{dim}Label"] = label
        result = report.to_dict(include_query=True)
        result["notes"] = notes
        if save_to:
            result["saved_to"] = ctx.save_rows(report.rows, save_to, default_stem="analytics")
        return result
