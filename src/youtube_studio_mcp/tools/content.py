"""Channel, videos, playlists and comments (YouTube Data API v3)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from youtube_studio_mcp.auth import AUTH_COMMAND, YOUTUBE_MANAGE
from youtube_studio_mcp.context import READ_ONLY, AppContext, SaveToArg, tool_errors
from youtube_studio_mcp.periods import parse_date
from youtube_studio_mcp.youtube import (
    get_videos,
    list_playlist_video_ids,
    summarize_video,
    to_int,
)

MAX_SCAN = 500
LIST_FIELDS_DROPPED = ("tags", "thumbnail_url", "category_id", "default_language", "made_for_kids")


def _topic_names(urls: list[str] | None) -> list[str]:
    return [url.rsplit("/", 1)[-1].replace("_", " ") for url in urls or []]


def _published_date(video: dict[str, Any]) -> str:
    return str(video.get("published_at") or "")[:10]


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(title="Channel overview", annotations=READ_ONLY)
    @tool_errors
    def get_channel() -> dict[str, Any]:
        """The signed-in channel's profile and lifetime public stats: subscribers, total views,
        video count, creation date, country, handle, description, keywords and topics."""
        channel = ctx.channel(fresh=True)
        snippet = channel.get("snippet") or {}
        stats = channel.get("statistics") or {}
        branding = (channel.get("brandingSettings") or {}).get("channel") or {}
        status = channel.get("status") or {}
        handle = snippet.get("customUrl")
        thumbnails = snippet.get("thumbnails") or {}
        return {
            "channel_id": channel.get("id"),
            "title": snippet.get("title"),
            "handle": handle,
            "url": f"https://www.youtube.com/{handle}"
            if handle
            else f"https://www.youtube.com/channel/{channel.get('id')}",
            "description": snippet.get("description"),
            "created_at": snippet.get("publishedAt"),
            "country": snippet.get("country") or branding.get("country"),
            "subscribers": to_int(stats.get("subscriberCount")),
            "subscriber_count_hidden": stats.get("hiddenSubscriberCount", False),
            "total_views": to_int(stats.get("viewCount")),
            "video_count": to_int(stats.get("videoCount")),
            "keywords": branding.get("keywords"),
            "topics": _topic_names((channel.get("topicDetails") or {}).get("topicCategories")),
            "made_for_kids": status.get("madeForKids"),
            "thumbnail_url": (thumbnails.get("high") or thumbnails.get("default") or {}).get("url"),
            "uploads_playlist_id": ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get(
                "uploads"
            ),
            "note": (
                "YouTube rounds public subscriber counts above 1,000 down to 3 significant figures "
                "(e.g. 123,456 -> 123,000). Use get_metrics_over_time with "
                "include_cumulative_subscribers for an exact-ish analytics-based count."
            ),
        }

    @mcp.tool(title="List videos", annotations=READ_ONLY)
    @tool_errors
    def list_videos(
        limit: Annotated[int, Field(ge=1, le=MAX_SCAN, description="Maximum videos to return.")] = 50,
        sort_by: Annotated[
            Literal["newest", "oldest", "most_viewed", "most_liked", "most_commented", "highest_engagement"],
            Field(description="Order of the returned videos (sorting happens over the scanned uploads)."),
        ] = "newest",
        published_after: Annotated[
            str | None, Field(description="Only videos published on/after YYYY-MM-DD.")
        ] = None,
        published_before: Annotated[
            str | None, Field(description="Only videos published on/before YYYY-MM-DD.")
        ] = None,
        content_format: Annotated[
            Literal["short", "video", "live"] | None,
            Field(
                description=(
                    "Filter by format using public metadata: 'short' = up to 3 minutes (likely Shorts), "
                    "'video' = longer uploads, 'live' = live streams/premieres."
                )
            ),
        ] = None,
        title_contains: Annotated[str | None, Field(description="Case-insensitive title filter.")] = None,
        playlist_id: Annotated[
            str | None, Field(description="List this playlist instead of the channel's uploads.")
        ] = None,
        save_to: SaveToArg = None,
    ) -> dict[str, Any]:
        """List the channel's uploads (including private/unlisted/scheduled, newest first) with
        public lifetime stats: views, likes, comments, engagement rate, duration, format guess.
        Costs ~1 quota unit per 50 videos. For period-specific performance use get_top_videos."""
        after = parse_date(published_after, "published_after") if published_after else None
        before = parse_date(published_before, "published_before") if published_before else None
        filtered = any([after, before, content_format, title_contains]) or sort_by != "newest"
        scan = MAX_SCAN if filtered else limit
        ids = list_playlist_video_ids(ctx.api, playlist_id or ctx.uploads_playlist_id(), scan)
        videos = [summarize_video(item) for item in get_videos(ctx.api, ids)]

        def keep(video: dict[str, Any]) -> bool:
            day = _published_date(video)
            if after and day and day < after.isoformat():
                return False
            if before and day and day > before.isoformat():
                return False
            if title_contains and title_contains.lower() not in str(video.get("title") or "").lower():
                return False
            if content_format == "short":
                return bool(video["format"] in {"short", "short_or_video"})
            if content_format == "video":
                return bool(video["format"] in {"video", "short_or_video"})
            if content_format == "live":
                return bool(video["format"] == "live")
            return True

        videos = [video for video in videos if keep(video)]
        sort_keys = {
            "newest": ("published_at", True),
            "oldest": ("published_at", False),
            "most_viewed": ("views", True),
            "most_liked": ("likes", True),
            "most_commented": ("comments", True),
            "highest_engagement": ("engagement_rate_pct", True),
        }
        key, reverse = sort_keys[sort_by]
        videos.sort(key=lambda v: (v.get(key) is not None, v.get(key) or 0), reverse=reverse)
        videos = videos[:limit]
        for video in videos:
            for dropped in LIST_FIELDS_DROPPED:
                video.pop(dropped, None)
        result: dict[str, Any] = {
            "count": len(videos),
            "scanned_uploads": len(ids),
            "videos": videos,
            "notes": [
                "Stats are lifetime public counts from the Data API (near real-time).",
                "format is a guess from duration: Shorts can be up to 3 minutes, so 61-180s videos are "
                "'short_or_video'. get_top_videos reports YouTube's own content type.",
            ],
        }
        if len(ids) >= MAX_SCAN and filtered:
            result["notes"].append(f"Only the newest {MAX_SCAN} uploads were scanned.")
        if save_to:
            result["saved_to"] = ctx.save_rows(videos, save_to, default_stem="videos")
        return result

    @mcp.tool(title="Video details", annotations=READ_ONLY)
    @tool_errors
    def get_videos_details(
        video_ids: Annotated[
            list[str], Field(min_length=1, max_length=50, description="Up to 50 video IDs.")
        ],
    ) -> dict[str, Any]:
        """Full metadata and lifetime public stats for specific videos: title, description, tags,
        category, privacy, duration, captions, live-stream details, views, likes, comments."""
        items = get_videos(
            ctx.api,
            video_ids,
            parts="snippet,contentDetails,statistics,status,liveStreamingDetails,topicDetails",
        )
        videos = []
        for item in items:
            summary = summarize_video(item)
            snippet = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            status = item.get("status") or {}
            summary.update(
                {
                    "description": snippet.get("description"),
                    "has_captions": details.get("caption") == "true",
                    "definition": details.get("definition"),
                    "scheduled_publish_at": status.get("publishAt"),
                    "embeddable": status.get("embeddable"),
                    "license": status.get("license"),
                    "live_streaming": item.get("liveStreamingDetails"),
                    "topics": _topic_names((item.get("topicDetails") or {}).get("topicCategories")),
                }
            )
            videos.append(summary)
        found = {video["video_id"] for video in videos}
        return {"videos": videos, "not_found": [vid for vid in video_ids if vid not in found]}

    @mcp.tool(title="List playlists", annotations=READ_ONLY)
    @tool_errors
    def list_playlists(
        limit: Annotated[int, Field(ge=1, le=200, description="Maximum playlists to return.")] = 50,
    ) -> dict[str, Any]:
        """The channel's playlists with item counts and privacy. Use list_videos(playlist_id=...)
        to see a playlist's videos."""
        playlists: list[dict[str, Any]] = []
        page_token: str | None = None
        while len(playlists) < limit:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails,status",
                "mine": True,
                "maxResults": min(50, limit - len(playlists)),
            }
            if page_token:
                params["pageToken"] = page_token
            response = ctx.api.data("playlists", "list", **params)
            for item in response.get("items") or []:
                snippet = item.get("snippet") or {}
                playlists.append(
                    {
                        "playlist_id": item.get("id"),
                        "title": snippet.get("title"),
                        "description": (snippet.get("description") or "")[:300],
                        "video_count": to_int((item.get("contentDetails") or {}).get("itemCount")),
                        "privacy_status": (item.get("status") or {}).get("privacyStatus"),
                        "published_at": snippet.get("publishedAt"),
                        "url": f"https://www.youtube.com/playlist?list={item.get('id')}",
                    }
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return {"count": len(playlists), "playlists": playlists}

    @mcp.tool(title="List comments", annotations=READ_ONLY)
    @tool_errors
    def list_comments(
        video_id: Annotated[
            str | None,
            Field(description="Comments on one video. Omit for recent comments across the channel."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum comment threads to return.")] = 50,
        order: Annotated[
            Literal["time", "relevance"], Field(description="Newest first, or YouTube's relevance.")
        ] = "time",
        search_terms: Annotated[str | None, Field(description="Only threads containing this text.")] = None,
        moderation_status: Annotated[
            Literal["published", "heldForReview", "likelySpam"],
            Field(
                description="'heldForReview' / 'likelySpam' show the comments waiting in Studio's review queue."
            ),
        ] = "published",
        include_replies: Annotated[bool, Field(description="Include up to 5 replies per thread.")] = False,
        save_to: SaveToArg = None,
    ) -> dict[str, Any]:
        """Read comment threads (top-level comments with reply counts) for the channel or one video,
        e.g. to summarise audience sentiment, find questions, or spot content ideas. Requires the
        optional 'manage' permission (`youtube-studio-mcp auth --comments`): YouTube has no
        read-only permission for comments."""
        granted = ctx.api.granted_scopes()
        if granted and YOUTUBE_MANAGE not in granted:
            raise ToolError(
                "Reading comments needs YouTube's 'manage' permission: Google has no read-only "
                "permission for comments. If the user wants the comment tools, they can run "
                f"`{AUTH_COMMAND} --comments` and approve it (editing tools still stay disabled "
                "unless YOUTUBE_STUDIO_MCP_ENABLE_WRITE=1)."
            )
        threads: list[dict[str, Any]] = []
        page_token: str | None = None
        channel_id = ctx.channel_id()
        while len(threads) < limit:
            params: dict[str, Any] = {
                "part": "snippet,replies" if include_replies else "snippet",
                "maxResults": min(100, limit - len(threads)),
                "order": order,
                "textFormat": "plainText",
                "moderationStatus": moderation_status,
            }
            if video_id:
                params["videoId"] = video_id
            else:
                params["allThreadsRelatedToChannelId"] = channel_id
            if search_terms:
                params["searchTerms"] = search_terms
            if page_token:
                params["pageToken"] = page_token
            response = ctx.api.data("commentThreads", "list", **params)
            for item in response.get("items") or []:
                snippet = item.get("snippet") or {}
                top = snippet.get("topLevelComment") or {}
                top_snippet = top.get("snippet") or {}
                author_channel = (top_snippet.get("authorChannelId") or {}).get("value")
                thread: dict[str, Any] = {
                    "comment_id": top.get("id"),
                    "video_id": snippet.get("videoId"),
                    "author": top_snippet.get("authorDisplayName"),
                    "author_is_channel_owner": author_channel == channel_id,
                    "text": top_snippet.get("textDisplay") or top_snippet.get("textOriginal"),
                    "likes": to_int(top_snippet.get("likeCount")),
                    "reply_count": to_int(snippet.get("totalReplyCount")),
                    "published_at": top_snippet.get("publishedAt"),
                    "edited": top_snippet.get("updatedAt") != top_snippet.get("publishedAt"),
                }
                if include_replies:
                    thread["replies"] = [
                        {
                            "comment_id": reply.get("id"),
                            "author": (reply.get("snippet") or {}).get("authorDisplayName"),
                            "author_is_channel_owner": (
                                ((reply.get("snippet") or {}).get("authorChannelId") or {}).get("value")
                                == channel_id
                            ),
                            "text": (reply.get("snippet") or {}).get("textDisplay"),
                            "likes": to_int((reply.get("snippet") or {}).get("likeCount")),
                            "published_at": (reply.get("snippet") or {}).get("publishedAt"),
                        }
                        for reply in ((item.get("replies") or {}).get("comments") or [])[:5]
                    ]
                threads.append(thread)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        result: dict[str, Any] = {
            "count": len(threads),
            "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "comments": threads,
        }
        if save_to:
            result["saved_to"] = ctx.save_rows(threads, save_to, default_stem="comments")
        return result
