"""YouTube Analytics API helpers: running reports, aggregating time series, labelling values."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from youtube_studio_mcp.periods import DateRange
from youtube_studio_mcp.youtube import YouTubeAPI, YouTubeAPIError

Granularity = Literal["day", "week", "month", "year"]

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

# Metrics that can be summed across days.
ADDITIVE_METRICS = frozenset(
    {
        "views",
        "engagedViews",
        "redViews",
        "estimatedMinutesWatched",
        "estimatedRedMinutesWatched",
        "subscribersGained",
        "subscribersLost",
        "likes",
        "dislikes",
        "comments",
        "shares",
        "videosAddedToPlaylists",
        "videosRemovedFromPlaylists",
        "estimatedRevenue",
        "estimatedAdRevenue",
        "grossRevenue",
        "estimatedRedPartnerRevenue",
        "monetizedPlaybacks",
        "adImpressions",
        "cardImpressions",
        "cardClicks",
        "cardTeaserImpressions",
        "cardTeaserClicks",
    }
)

# Averages/ratios that aggregate exactly as a weighted mean of daily values.
WEIGHTED_METRICS: dict[str, str] = {
    "averageViewDuration": "views",
    "averageViewPercentage": "views",
    "cpm": "adImpressions",
    "playbackBasedCpm": "monetizedPlaybacks",
    "cardClickRate": "cardImpressions",
    "cardTeaserClickRate": "cardTeaserImpressions",
}

REVENUE_METRICS = (
    "estimatedRevenue",
    "estimatedAdRevenue",
    "grossRevenue",
    "monetizedPlaybacks",
    "adImpressions",
    "cpm",
    "playbackBasedCpm",
)

CORE_METRICS = (
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
    "subscribersLost",
    "likes",
    "comments",
    "shares",
)

TRAFFIC_SOURCE_LABELS = {
    "ADVERTISING": "YouTube advertising",
    "ANNOTATION": "Annotations",
    "CAMPAIGN_CARD": "Campaign cards",
    "END_SCREEN": "End screens",
    "EXT_URL": "External websites & apps",
    "HASHTAGS": "Hashtag pages",
    "LIVE_REDIRECT": "Live redirects",
    "NO_LINK_EMBEDDED": "Embedded players (no link)",
    "NO_LINK_OTHER": "Direct or unknown",
    "NOTIFICATION": "Notifications",
    "PLAYLIST": "Playlists",
    "PRODUCT_PAGE": "Product pages",
    "PROMOTED": "Promoted (unpaid)",
    "RELATED_VIDEO": "Suggested videos",
    "SHORTS": "Shorts feed",
    "SOUND_PAGE": "Sound pages",
    "SUBSCRIBER": "Browse features (home, subscriptions)",
    "YT_CHANNEL": "Channel pages",
    "YT_OTHER_PAGE": "Other YouTube features",
    "YT_SEARCH": "YouTube search",
    "VIDEO_REMIXES": "Remixes",
    "WATCH_WITH": "Watch With",
}

VALUE_LABELS: dict[str, dict[str, str]] = {
    "insightTrafficSourceType": TRAFFIC_SOURCE_LABELS,
    "creatorContentType": {
        "SHORTS": "Shorts",
        "VIDEO_ON_DEMAND": "Videos",
        "LIVE_STREAM": "Live",
        "STORY": "Stories",
        "UNSPECIFIED": "Unspecified",
    },
    "deviceType": {
        "MOBILE": "Mobile phone",
        "DESKTOP": "Computer",
        "TV": "TV",
        "TABLET": "Tablet",
        "GAME_CONSOLE": "Game console",
        "AUTOMOTIVE": "Car",
        "WEARABLE": "Wearable",
        "UNKNOWN_PLATFORM": "Unknown",
    },
    "subscribedStatus": {"SUBSCRIBED": "Subscribers", "UNSUBSCRIBED": "Not subscribed"},
    "insightPlaybackLocationType": {
        "BROWSE": "Browse features",
        "CHANNEL": "Channel page",
        "EMBEDDED": "Embedded on other sites",
        "EXTERNAL_APP": "External apps",
        "IMMERSIVE_LIVE_FEED": "Live feed",
        "MOBILE": "YouTube mobile (legacy)",
        "SEARCH": "Search results",
        "SHORTS_FEED": "Shorts feed",
        "WATCH": "Watch page",
        "YT_OTHER": "Other YouTube pages",
    },
    "gender": {"female": "Female", "male": "Male", "user_specified": "User-specified"},
    "ageGroup": {
        "age13-17": "13-17",
        "age18-24": "18-24",
        "age25-34": "25-34",
        "age35-44": "35-44",
        "age45-54": "45-54",
        "age55-64": "55-64",
        "age65-": "65+",
    },
}


def label_for(dimension: str, value: Any) -> str | None:
    """Human-friendly label for a dimension value, or None if there is no mapping."""
    mapping = VALUE_LABELS.get(dimension)
    if mapping is None or value is None:
        return None
    return mapping.get(str(value), str(value).replace("_", " ").title())


@dataclass
class Report:
    columns: list[str]
    rows: list[dict[str, Any]]
    date_range: DateRange
    query: dict[str, Any]

    def to_dict(self, *, include_query: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "date_range": self.date_range.to_dict(),
            "row_count": len(self.rows),
            "rows": self.rows,
        }
        if include_query:
            out["query"] = self.query
        return out


def _validate_names(kind: str, names: Iterable[str]) -> list[str]:
    cleaned = [name.strip() for name in names if name and name.strip()]
    for name in cleaned:
        if not _NAME_RE.match(name):
            raise ValueError(f"Invalid {kind} name {name!r}; use API names such as 'views' or 'day'.")
    return cleaned


def run_report(
    api: YouTubeAPI,
    date_range: DateRange,
    metrics: Sequence[str],
    *,
    dimensions: Sequence[str] = (),
    filters: str | None = None,
    sort: str | None = None,
    max_results: int | None = None,
    currency: str | None = None,
    start_index: int | None = None,
) -> Report:
    """Run one ``reports.query`` call for the authenticated channel."""
    metric_names = _validate_names("metric", metrics)
    if not metric_names:
        raise ValueError("At least one metric is required.")
    dimension_names = _validate_names("dimension", dimensions)
    params: dict[str, Any] = {
        "ids": "channel==MINE",
        "startDate": date_range.start.isoformat(),
        "endDate": date_range.end.isoformat(),
        "metrics": ",".join(metric_names),
    }
    if dimension_names:
        params["dimensions"] = ",".join(dimension_names)
    if filters:
        params["filters"] = filters
    if sort:
        params["sort"] = sort
    if max_results is not None:
        params["maxResults"] = int(max_results)
    if currency:
        params["currency"] = currency.upper()
    if start_index is not None:
        params["startIndex"] = int(start_index)

    response = api.analytics(**params)
    columns = [str(header.get("name")) for header in response.get("columnHeaders") or []]
    rows = [dict(zip(columns, row, strict=False)) for row in response.get("rows") or []]
    return Report(columns=columns, rows=rows, date_range=date_range, query=params)


def run_report_with_optional_metrics(
    api: YouTubeAPI,
    date_range: DateRange,
    required: Sequence[str],
    optional: Sequence[str],
    **kwargs: Any,
) -> Report:
    """Run a report; if YouTube rejects the query (HTTP 400), retry without ``optional`` metrics.

    Used for metrics that are documented but not available to every channel or date range
    (e.g. ``engagedViews`` before April 2025).
    """
    if optional:
        try:
            return run_report(api, date_range, [*required, *optional], **kwargs)
        except YouTubeAPIError as exc:
            if exc.status != 400:
                raise
    return run_report(api, date_range, required, **kwargs)


# --------------------------------------------------------------------------- time aggregation


def bucket_start(day: date, granularity: Granularity) -> date:
    if granularity == "day":
        return day
    if granularity == "week":
        return day - timedelta(days=day.weekday())  # ISO weeks start on Monday
    if granularity == "month":
        return day.replace(day=1)
    return date(day.year, 1, 1)


def bucket_label(start: date, granularity: Granularity) -> str:
    if granularity == "month":
        return start.strftime("%Y-%m")
    if granularity == "year":
        return str(start.year)
    return start.isoformat()


def weight_metrics_needed(metrics: Sequence[str]) -> list[str]:
    """Metrics that must also be queried so weighted averages can be aggregated."""
    return [
        WEIGHTED_METRICS[metric]
        for metric in metrics
        if metric in WEIGHTED_METRICS and WEIGHTED_METRICS[metric] not in metrics
    ]


def aggregate_daily_rows(
    rows: Sequence[dict[str, Any]],
    metrics: Sequence[str],
    granularity: Granularity,
    *,
    date_range: DateRange,
    group_by: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Aggregate rows that have a ``day`` column into week/month/year buckets.

    Additive metrics are summed. Averages and ratios are weighted means over days
    (e.g. ``averageViewDuration`` weighted by ``views``), which matches how YouTube
    computes them. Buckets that are cut off by the date range are flagged ``partial``.
    """
    unsupported = [m for m in metrics if m not in ADDITIVE_METRICS and m not in WEIGHTED_METRICS]
    if granularity != "day" and unsupported:
        raise ValueError(
            f"Cannot aggregate {', '.join(unsupported)} by {granularity}; use granularity='day' "
            "or query_analytics."
        )

    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    weights: dict[tuple[Any, ...], dict[str, float]] = {}
    for row in rows:
        day = date.fromisoformat(str(row["day"]))
        start = bucket_start(day, granularity)
        key = (start, *(row.get(dim) for dim in group_by))
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {"period": bucket_label(start, granularity), "_start": start}
            for dim in group_by:
                bucket[dim] = row.get(dim)
            for metric in metrics:
                bucket[metric] = 0.0
            buckets[key] = bucket
            weights[key] = dict.fromkeys(metrics, 0.0)
        for metric in metrics:
            value = row.get(metric)
            if value is None:
                continue
            if metric in WEIGHTED_METRICS:
                weight = float(row.get(WEIGHTED_METRICS[metric]) or 0)
                bucket[metric] += float(value) * weight
                weights[key][metric] += weight
            else:
                bucket[metric] += float(value)

    out: list[dict[str, Any]] = []
    for key in sorted(buckets, key=lambda k: tuple(str(part) for part in k)):
        bucket = buckets[key]
        start = bucket.pop("_start")
        for metric in metrics:
            if metric in WEIGHTED_METRICS:
                total_weight = weights[key][metric]
                bucket[metric] = round(bucket[metric] / total_weight, 4) if total_weight else None
            else:
                bucket[metric] = _tidy_number(bucket[metric])
        if granularity != "day":
            bucket_end = _bucket_end(start, granularity)
            bucket["partial"] = start < date_range.start or bucket_end > date_range.end
        out.append(bucket)
    return out


def _bucket_end(start: date, granularity: Granularity) -> date:
    if granularity == "week":
        return start + timedelta(days=6)
    if granularity == "month":
        next_month = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        return next_month - timedelta(days=1)
    if granularity == "year":
        return date(start.year, 12, 31)
    return start


def _tidy_number(value: float) -> float | int:
    rounded = round(value, 4)
    return int(rounded) if float(rounded).is_integer() else rounded


def aggregate_totals(
    rows: Sequence[dict[str, Any]], metrics: Sequence[str], *, group_by: str
) -> dict[Any, dict[str, Any]]:
    """Collapse rows into one total per ``group_by`` value (sums; weighted means for averages)."""
    sums: dict[Any, dict[str, float]] = {}
    weights: dict[Any, dict[str, float]] = {}
    for row in rows:
        key = row.get(group_by)
        bucket = sums.setdefault(key, dict.fromkeys(metrics, 0.0))
        weight_bucket = weights.setdefault(key, dict.fromkeys(metrics, 0.0))
        for metric in metrics:
            value = row.get(metric)
            if value is None:
                continue
            if metric in WEIGHTED_METRICS:
                weight = float(row.get(WEIGHTED_METRICS[metric]) or 0)
                bucket[metric] += float(value) * weight
                weight_bucket[metric] += weight
            else:
                bucket[metric] += float(value)
    out: dict[Any, dict[str, Any]] = {}
    for key, bucket in sums.items():
        out[key] = {
            metric: (
                (round(bucket[metric] / weights[key][metric], 4) if weights[key][metric] else None)
                if metric in WEIGHTED_METRICS
                else _tidy_number(bucket[metric])
            )
            for metric in metrics
        }
    return out


def add_shares(rows: list[dict[str, Any]], metric: str, *, key: str = "share_pct") -> None:
    """Add each row's percentage of the column total (in place)."""
    total = sum(float(row.get(metric) or 0) for row in rows)
    for row in rows:
        row[key] = round(float(row.get(metric) or 0) / total * 100, 2) if total else None


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    assert previous is not None
    return round((current - previous) / abs(previous) * 100, 1)


def video_filter(video_ids: Sequence[str] | None) -> str | None:
    if not video_ids:
        return None
    ids = [vid.strip() for vid in video_ids if vid and vid.strip()]
    if len(ids) > 500:
        raise ValueError("At most 500 video IDs can be used in one analytics filter.")
    return "video==" + ",".join(ids)


def join_filters(*parts: str | None) -> str | None:
    present = [part for part in parts if part]
    return ";".join(present) if present else None
