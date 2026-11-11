"""A deterministic, rule-checking fake of the YouTube Data, Analytics and Reporting APIs.

It produces plausible numbers for a fictional channel and rejects the query shapes the real
Analytics API rejects (so tests catch invalid dimension/metric/filter combinations).
"""

from __future__ import annotations

import hashlib
import math
import tempfile
from datetime import date, datetime, time, timedelta
from itertools import product
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from youtube_studio_mcp.auth import (
    ANALYTICS_MONETARY_READONLY,
    ANALYTICS_READONLY,
    YOUTUBE_READONLY,
    load_credentials,
)
from youtube_studio_mcp.config import Settings
from youtube_studio_mcp.youtube import YouTubeAPIError

PACIFIC = ZoneInfo("America/Los_Angeles")
CHANNEL_ID = "UCfakeChannel000000000001"
UPLOADS = "UUfakeChannel000000000001"
REACH_JOB = {
    "id": "job-reach-1",
    "reportTypeId": "channel_reach_basic_a1",
    "createTime": "2026-09-01T10:00:00Z",
}

TITLES = [
    "How I plan a whole month of content in one afternoon",
    "5 beginner mistakes I wish I'd known",
    "A slow Sunday studio vlog",
    "Testing viral hacks so you don't have to",
    "My honest review after 6 months",
    "The easiest project for complete beginners",
    "Studio tour 2026",
    "What I earned from my first year on YouTube",
    "Answering your most asked questions",
    "Trying a new technique live",
]

DIM_VALUES: dict[str, list[str]] = {
    # The real API returns camelCase values here (not the documented SHORTS/VIDEO_ON_DEMAND).
    "creatorContentType": ["videoOnDemand", "shorts", "liveStream", "posts"],
    "ageGroup": ["age13-17", "age18-24", "age25-34", "age35-44", "age45-54", "age55-64", "age65-"],
    "gender": ["female", "male", "user_specified"],
    "country": ["US", "FR", "CA", "GB", "BE", "CH", "DE", "AU", "ES", "IT"],
    "province": ["US-CA", "US-NY", "US-TX", "US-FL"],
    "deviceType": ["MOBILE", "TV", "DESKTOP", "TABLET"],
    "operatingSystem": ["IOS", "ANDROID", "WINDOWS", "MACINTOSH"],
    "subscribedStatus": ["UNSUBSCRIBED", "SUBSCRIBED"],
    "insightPlaybackLocationType": ["SHORTS_FEED", "WATCH", "BROWSE", "EMBEDDED"],
    "insightTrafficSourceType": [
        "SHORTS",
        "SUBSCRIBER",
        "RELATED_VIDEO",
        "YT_SEARCH",
        "YT_CHANNEL",
        "NO_LINK_OTHER",
        "EXT_URL",
        "PLAYLIST",
        "NOTIFICATION",
    ],
    "sharingService": ["WHATS_APP", "COPY_PASTE", "INSTAGRAM"],
}
SHARES: dict[str, list[float]] = {
    "creatorContentType": [0.40, 0.56, 0.02, 0.02],
    "ageGroup": [0.04, 0.22, 0.36, 0.20, 0.10, 0.05, 0.03],
    "gender": [0.68, 0.31, 0.01],
    "country": [0.38, 0.21, 0.09, 0.07, 0.05, 0.04, 0.03, 0.03, 0.02, 0.02],
    "province": [0.3, 0.25, 0.25, 0.2],
    "deviceType": [0.71, 0.14, 0.12, 0.03],
    "operatingSystem": [0.45, 0.3, 0.15, 0.1],
    "subscribedStatus": [0.72, 0.28],
    "insightPlaybackLocationType": [0.5, 0.35, 0.12, 0.03],
    "insightTrafficSourceType": [0.42, 0.21, 0.14, 0.09, 0.05, 0.04, 0.02, 0.02, 0.01],
    "sharingService": [0.5, 0.3, 0.2],
}
PER_VIEW = {
    "engagedViews": 0.84,
    "redViews": 0.01,
    "estimatedMinutesWatched": 2.4,
    "likes": 0.048,
    "dislikes": 0.0012,
    "comments": 0.0041,
    "shares": 0.0032,
    "subscribersGained": 0.0177,
    "subscribersLost": 0.0042,
    "videosAddedToPlaylists": 0.002,
    "videosRemovedFromPlaylists": 0.0004,
    "estimatedRevenue": 0.0041,
    "estimatedAdRevenue": 0.0033,
    "grossRevenue": 0.0061,
    "monetizedPlaybacks": 0.31,
    "adImpressions": 0.52,
}
AVERAGES = {"averageViewDuration": 144.0, "averageViewPercentage": 47.5, "cpm": 11.7, "playbackBasedCpm": 9.8}
API_CONTENT_TYPE = {"SHORTS": "shorts", "VIDEO_ON_DEMAND": "videoOnDemand"}
REVENUE = {
    "estimatedRevenue",
    "estimatedAdRevenue",
    "grossRevenue",
    "monetizedPlaybacks",
    "adImpressions",
    "cpm",
    "playbackBasedCpm",
}


def _noise(*parts: object) -> float:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF  # 0..1


def bad_request(message: str) -> YouTubeAPIError:
    return YouTubeAPIError(f"YouTube Analytics API error 400: {message}", status=400, reasons=["badRequest"])


class FakeYouTubeAPI:
    def __init__(
        self,
        *,
        today: date = date(2026, 11, 11),
        monetized: bool = True,
        reach_enabled: bool = True,
        engaged_views_supported: bool = True,
        granted: list[str] | None = None,
        signed_in: bool = True,
        video_count: int = 60,
    ) -> None:
        self.today = today
        self.monetized = monetized
        self.reach_enabled = reach_enabled
        self.engaged_views_supported = engaged_views_supported
        self.granted = (
            granted
            if granted is not None
            else [YOUTUBE_READONLY, ANALYTICS_READONLY, ANALYTICS_MONETARY_READONLY]
        )
        self.signed_in = signed_in
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.created_jobs: list[dict[str, Any]] = []
        self.videos = self._catalog(video_count)

    # ------------------------------------------------------------------ catalog

    def _catalog(self, count: int) -> list[dict[str, Any]]:
        videos = []
        for n in range(count):
            published = datetime.combine(self.today - timedelta(days=2 + n * 4), time(16, 0))
            is_short = n % 2 == 1
            duration = 20 + (n * 7) % 40 if is_short else 480 + (n * 97) % 900
            views = int((40_000 if not is_short else 25_000) * (0.4 + _noise("v", n)) * (1 + 1 / (n + 1)))
            vid = f"vid{n:08d}"
            videos.append(
                {
                    "id": vid,
                    "kind": "SHORTS" if is_short else "VIDEO_ON_DEMAND",
                    "snippet": {
                        "title": ("#shorts " if is_short else "") + TITLES[n % len(TITLES)],
                        "description": "Description of the video.",
                        "publishedAt": published.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "tags": ["tutorial", "studio"],
                        "categoryId": "26",
                        "defaultAudioLanguage": "fr",
                        "liveBroadcastContent": "none",
                        "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}},
                    },
                    "contentDetails": {
                        "duration": f"PT{duration // 60}M{duration % 60}S",
                        "caption": "true",
                        "definition": "hd",
                    },
                    "statistics": {
                        "viewCount": str(views),
                        "likeCount": str(int(views * 0.05)),
                        "commentCount": str(int(views * 0.004)),
                    },
                    "status": {
                        "privacyStatus": "public" if n % 13 else "unlisted",
                        "embeddable": True,
                        "license": "youtube",
                        "publicStatsViewable": True,
                        "madeForKids": False,
                        "selfDeclaredMadeForKids": False,
                    },
                }
            )
        return videos

    def _video_resource(self, video: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in video.items() if k != "kind"}

    # ------------------------------------------------------------------ protocol

    def granted_scopes(self) -> list[str]:
        self._check_auth()
        return list(self.granted)

    def reset(self) -> None:
        self.calls.append(("reset", {}))

    def _check_auth(self) -> None:
        if not self.signed_in:
            # Use the real loader so tests see the production error message.
            load_credentials(Settings(config_dir=Path(tempfile.gettempdir()) / "ysm-tests-no-such-dir"))

    def data(self, resource: str, method: str, **params: Any) -> dict[str, Any]:
        self._check_auth()
        self.calls.append((f"data.{resource}.{method}", params))
        handler = getattr(self, f"_data_{resource}_{method}")
        result: dict[str, Any] = handler(**params)
        return result

    def reporting(self, resource: str, method: str, **params: Any) -> dict[str, Any]:
        self._check_auth()
        self.calls.append((f"reporting.{resource}.{method}", params))
        if (resource, method) == ("jobs", "list"):
            return {
                "jobs": [REACH_JOB, *self.created_jobs] if self.reach_enabled else list(self.created_jobs)
            }
        if (resource, method) == ("reportTypes", "list"):
            return {"reportTypes": [{"id": "channel_basic_a3"}, {"id": "channel_reach_basic_a1"}]}
        if (resource, method) == ("jobs", "create"):
            job = {"id": "job-new", "createTime": "2026-11-11T00:00:00Z", **params["body"]}
            self.created_jobs.append(job)
            return job
        if (resource, method) == ("jobs.reports", "list"):
            reports = []
            first = date(2026, 8, 2)  # 30 days before the job was created
            day = first
            while day <= self.today - timedelta(days=2):
                start = datetime.combine(day, time(0), tzinfo=PACIFIC).astimezone(ZoneInfo("UTC"))
                reports.append(
                    {
                        "id": f"r{day:%Y%m%d}",
                        "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "endTime": (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "createTime": f"{day + timedelta(days=2)}T10:00:00Z",
                        "downloadUrl": f"https://youtubereporting.googleapis.com/v1/media/CHANNEL/{day:%Y%m%d}?alt=media",
                    }
                )
                day += timedelta(days=1)
            return {"reports": reports}
        raise AssertionError(f"unexpected reporting call {resource}.{method}")

    def download(self, url: str) -> bytes:
        self.calls.append(("download", {"url": url}))
        day = url.rsplit("/", 1)[-1].split("?")[0]
        lines = ["date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr"]
        for n, video in enumerate(self.videos[:20]):
            impressions = int(3000 * (0.5 + _noise("imp", day, n)) / (1 + n / 4))
            ctr = round(3 + 6 * _noise("ctr", day, n), 2)  # percent scale, like Studio
            lines.append(f"{day},{CHANNEL_ID},{video['id']},{impressions},{ctr}")
        return ("\n".join(lines) + "\n").encode()

    # ------------------------------------------------------------------ Data API handlers

    def _data_channels_list(self, **params: Any) -> dict[str, Any]:
        assert params.get("mine") is True
        return {
            "items": [
                {
                    "id": CHANNEL_ID,
                    "snippet": {
                        "title": "Maple & Thread",
                        "description": "Slow crafting, honest tutorials and studio vlogs.",
                        "customUrl": "@mapleandthread",
                        "publishedAt": "2019-03-02T10:00:00Z",
                        "country": "FR",
                        "thumbnails": {"high": {"url": "https://yt3.ggpht.com/fake-avatar=s800"}},
                    },
                    "statistics": {
                        "subscriberCount": "48400",
                        "viewCount": "9120345",
                        "videoCount": "412",
                        "hiddenSubscriberCount": False,
                    },
                    "contentDetails": {"relatedPlaylists": {"uploads": UPLOADS}},
                    "brandingSettings": {"channel": {"keywords": "crafts tutorials vlog"}},
                    "status": {"madeForKids": False},
                    "topicDetails": {
                        "topicCategories": [
                            "https://en.wikipedia.org/wiki/Hobby",
                            "https://en.wikipedia.org/wiki/Lifestyle_(sociology)",
                        ]
                    },
                }
            ]
        }

    def _data_playlistItems_list(self, **params: Any) -> dict[str, Any]:
        assert params["maxResults"] <= 50
        start = int(params.get("pageToken") or 0)
        chunk = self.videos[start : start + params["maxResults"]]
        response: dict[str, Any] = {
            "items": [
                {"contentDetails": {"videoId": v["id"], "videoPublishedAt": v["snippet"]["publishedAt"]}}
                for v in chunk
            ]
        }
        if start + params["maxResults"] < len(self.videos):
            response["nextPageToken"] = str(start + params["maxResults"])
        return response

    def _data_videos_list(self, **params: Any) -> dict[str, Any]:
        ids = params["id"].split(",")
        assert len(ids) <= 50, "videos.list accepts at most 50 IDs"
        by_id = {v["id"]: v for v in self.videos}
        return {"items": [self._video_resource(by_id[i]) for i in ids if i in by_id]}

    def _data_videos_update(self, **params: Any) -> dict[str, Any]:
        body = params["body"]
        assert "title" in body["snippet"] and "categoryId" in body["snippet"], (
            "snippet must keep title/categoryId"
        )
        return body

    def _data_playlists_list(self, **params: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": f"PL{n}",
                    "snippet": {
                        "title": f"Playlist {n}",
                        "description": "",
                        "publishedAt": "2024-01-01T00:00:00Z",
                    },
                    "contentDetails": {"itemCount": 10 + n},
                    "status": {"privacyStatus": "public"},
                }
                for n in range(3)
            ]
        }

    def _data_commentThreads_list(self, **params: Any) -> dict[str, Any]:
        if "https://www.googleapis.com/auth/youtube.force-ssl" not in self.granted:
            raise YouTubeAPIError(
                "YouTube Data API error 403: Request had insufficient authentication scopes.",
                status=403,
                reasons=["insufficientPermissions"],
            )
        assert ("videoId" in params) != ("allThreadsRelatedToChannelId" in params)
        assert params["maxResults"] <= 100
        items = []
        for n in range(min(params["maxResults"], 12)):
            items.append(
                {
                    "id": f"thread{n}",
                    "snippet": {
                        "videoId": params.get("videoId", self.videos[n % 5]["id"]),
                        "totalReplyCount": n % 3,
                        "topLevelComment": {
                            "id": f"comment{n}",
                            "snippet": {
                                "authorDisplayName": f"Viewer {n}",
                                "authorChannelId": {"value": f"UCviewer{n}"},
                                "textDisplay": "Loved this! Could you do a follow-up?"
                                if n % 2
                                else "Great tips, thanks",
                                "likeCount": n,
                                "publishedAt": "2026-11-01T10:00:00Z",
                                "updatedAt": "2026-11-01T10:00:00Z",
                            },
                        },
                    },
                    "replies": {
                        "comments": [
                            {
                                "id": f"reply{n}",
                                "snippet": {
                                    "authorDisplayName": "Maple & Thread",
                                    "authorChannelId": {"value": CHANNEL_ID},
                                    "textDisplay": "Thank you!",
                                    "likeCount": 1,
                                    "publishedAt": "2026-11-02T10:00:00Z",
                                },
                            }
                        ]
                    },
                }
            )
        return {"items": items}

    def _data_comments_insert(self, **params: Any) -> dict[str, Any]:
        return {"id": "newreply1", **params["body"]}

    def _data_comments_setModerationStatus(self, **params: Any) -> dict[str, Any]:
        return {}

    # ------------------------------------------------------------------ Analytics API

    def analytics(self, **params: Any) -> dict[str, Any]:
        self._check_auth()
        self.calls.append(("analytics", params))
        assert params["ids"] == "channel==MINE"
        start = date.fromisoformat(params["startDate"])
        end = date.fromisoformat(params["endDate"])
        if start > end:
            raise bad_request("startDate after endDate")
        metrics = params["metrics"].split(",")
        dims = params["dimensions"].split(",") if params.get("dimensions") else []
        filters = (
            dict(f.split("==", 1) for f in params["filters"].split(";")) if params.get("filters") else {}
        )
        sort = params.get("sort")
        max_results = params.get("maxResults")
        self._validate(start, end, metrics, dims, filters, sort, max_results)
        rows = self._rows(start, end, metrics, dims, filters)
        if sort:
            for key in reversed(sort.split(",")):
                name = key.lstrip("-")
                rows.sort(
                    key=lambda r, name=name: r[(dims + metrics).index(name)], reverse=key.startswith("-")
                )
        if max_results:
            rows = rows[:max_results]
        return {"columnHeaders": [{"name": n} for n in dims + metrics], "rows": rows}

    def _validate(self, start, end, metrics, dims, filters, sort, max_results) -> None:  # type: ignore[no-untyped-def]
        if "engagedViews" in metrics and not self.engaged_views_supported:
            raise bad_request("Unknown identifier (engagedViews)")
        if REVENUE & set(metrics) and not self.monetized:
            raise YouTubeAPIError(
                "YouTube Analytics API error 403: Forbidden", status=403, reasons=["forbidden"]
            )
        demographic = {"ageGroup", "gender"} & set(dims)
        if demographic and metrics != ["viewerPercentage"]:
            raise bad_request("demographics only support viewerPercentage")
        if "viewerPercentage" in metrics and not demographic:
            raise bad_request("viewerPercentage requires ageGroup or gender")
        if "month" in dims and (start.day != 1 or end.day != 1):
            raise bad_request("month dimension requires dates on the first day of a month")
        if "video" in dims and "video" not in filters and (not sort or not max_results or max_results > 200):
            raise bad_request("video dimension requires sort and maxResults <= 200")
        if "insightTrafficSourceDetail" in dims and (
            "insightTrafficSourceType" not in filters or not sort or not max_results or max_results > 25
        ):
            raise bad_request("insightTrafficSourceDetail needs a source filter, sort and maxResults <= 25")
        if "province" in dims and filters.get("country") != "US":
            raise bad_request("province requires country==US")
        if "elapsedVideoTimeRatio" in dims and ("video" not in filters or "," in filters["video"]):
            raise bad_request("retention requires exactly one video filter")
        if "video" in dims and "creatorContentType" in dims:
            raise bad_request("The query is not supported.")  # verified against the live API
        if "creatorContentType" in filters:
            for value in filters["creatorContentType"].split(","):
                if value not in DIM_VALUES["creatorContentType"]:
                    raise bad_request(f"Invalid value ({value}) given in field parameters.filters.")
        if "subscribersGained" in metrics and "subscribedStatus" in dims:
            raise bad_request("The query is not supported.")
        if (
            "creatorContentType" in dims
            and len(dims) == 1
            and {"comments", "subscribersGained", "subscribersLost"} & set(metrics)
        ):
            raise bad_request("The query is not supported.")
        for name in dims:
            if name not in DIM_VALUES and name not in {
                "day",
                "month",
                "video",
                "insightTrafficSourceDetail",
                "elapsedVideoTimeRatio",
            }:
                raise bad_request(f"Unknown dimension {name}")

    def _day_views(self, day: date) -> float:
        weekly = 1 + 0.15 * math.sin(day.toordinal() / 7 * 2 * math.pi)
        trend = 1 + (day - date(2025, 1, 1)).days / 900
        return max(0.0, 2600 * weekly * trend * (0.85 + 0.3 * _noise("d", day)))

    def _dimension_values(
        self, name: str, start: date, end: date, filters: dict[str, str]
    ) -> list[tuple[str, float]]:
        if name == "day":
            return [((start + timedelta(days=i)).isoformat(), -1.0) for i in range((end - start).days + 1)]
        if name == "month":
            months, cursor = [], start
            while cursor <= end:
                months.append((cursor.strftime("%Y-%m"), -1.0))
                cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
            return months
        if name == "video":
            pool = [v for v in self.videos if v["snippet"]["publishedAt"][:10] <= end.isoformat()]
            if "video" in filters:
                wanted = filters["video"].split(",")
                pool = [v for v in pool if v["id"] in wanted]
            if "creatorContentType" in filters:
                kinds = filters["creatorContentType"].split(",")
                pool = [v for v in pool if API_CONTENT_TYPE[v["kind"]] in kinds]
            weights = [1 / (i + 1) ** 0.9 for i in range(len(pool))]
            total = sum(weights) or 1
            return [(v["id"], w / total) for v, w in zip(pool, weights, strict=True)]
        if name == "insightTrafficSourceDetail":
            source = filters.get("insightTrafficSourceType")
            if source == "YT_SEARCH":
                terms = [
                    "easy beginner project",
                    "studio tour",
                    "how to start",
                    "slow living vlog",
                    "honest review",
                ]
                return [(t, 0.3 / (i + 1)) for i, t in enumerate(terms)]
            return [(f"vidother{i:04d}", 0.2 / (i + 1)) for i in range(8)]
        if name == "elapsedVideoTimeRatio":
            return [(f"{(i + 1) / 100:.2f}", -1.0) for i in range(100)]
        return list(zip(DIM_VALUES[name], SHARES[name], strict=True))

    def _rows(self, start, end, metrics, dims, filters) -> list[list[Any]]:  # type: ignore[no-untyped-def]
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        scale = 0.08 if "video" in filters and "video" not in dims else 1.0
        if "country" in filters and "province" not in dims:
            scale *= 0.38
        if "creatorContentType" in filters and "video" not in dims:
            type_shares = dict(
                zip(DIM_VALUES["creatorContentType"], SHARES["creatorContentType"], strict=True)
            )
            scale *= sum(type_shares[v] for v in filters["creatorContentType"].split(","))
        value_lists = [self._dimension_values(d, start, end, filters) for d in dims]
        rows = []
        for combo in product(*value_lists) if dims else [()]:
            labels = [value for value, _ in combo]
            share = math.prod(weight for _, weight in combo if weight >= 0) if combo else 1.0
            if "day" in dims:
                period_days = [date.fromisoformat(labels[dims.index("day")])]
            elif "month" in dims:
                month = labels[dims.index("month")]
                period_days = [d for d in days if d.strftime("%Y-%m") == month]
            else:
                period_days = days
            views = sum(self._day_views(d) for d in period_days) * share * scale
            row: list[Any] = list(labels)
            for metric in metrics:
                row.append(self._metric(metric, views, labels, dims, combo))
            rows.append(row)
        return rows

    def _metric(self, metric: str, views: float, labels: list[str], dims: list[str], combo: Any) -> Any:
        content = labels[dims.index("creatorContentType")] if "creatorContentType" in dims else None
        if metric == "views":
            return round(views)
        if metric in PER_VIEW:
            factor = PER_VIEW[metric]
            if metric == "estimatedMinutesWatched" and content:
                factor = {"shorts": 0.38, "videoOnDemand": 5.1, "liveStream": 9.0, "posts": 0.05}.get(
                    content, factor
                )
            value = views * factor
            return (
                round(value, 3)
                if metric in {"estimatedRevenue", "estimatedAdRevenue", "grossRevenue"}
                else round(value)
            )
        if metric in AVERAGES:
            base = AVERAGES[metric]
            if metric == "averageViewDuration" and content:
                base = {"shorts": 23.0, "videoOnDemand": 306.0, "liveStream": 540.0, "posts": 4.0}.get(
                    content, base
                )
            return round(base * (0.9 + 0.2 * _noise(metric, *labels)), 2)
        if metric == "viewerPercentage":
            return round(math.prod(w for _, w in combo) * 100, 2)
        if metric == "audienceWatchRatio":
            ratio = float(labels[dims.index("elapsedVideoTimeRatio")])
            return round(0.95 * math.exp(-1.1 * ratio) + (0.08 if 0.4 < ratio < 0.45 else 0), 4)
        if metric == "relativeRetentionPerformance":
            return round(0.52 + 0.1 * _noise("rel", *labels), 4)
        raise bad_request(f"Unknown metric {metric}")
