"""Opt-in tools that change things on YouTube (enabled with YOUTUBE_STUDIO_MCP_ENABLE_WRITE=1)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from youtube_studio_mcp.auth import AUTH_COMMAND, YOUTUBE_MANAGE
from youtube_studio_mcp.context import AppContext, require_scope, tool_errors

MAX_TITLE = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500

EDIT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)
ADD = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)

# Status fields that videos.update resets to defaults unless they are sent back.
_STATUS_FIELDS = (
    "privacyStatus",
    "embeddable",
    "license",
    "publicStatsViewable",
    "publishAt",
    "selfDeclaredMadeForKids",
    "containsSyntheticMedia",
)
_SNIPPET_FIELDS = ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")


def _needs_write_scope(ctx: AppContext) -> None:
    require_scope(ctx, YOUTUBE_MANAGE, purpose="Editing on YouTube", command=f"{AUTH_COMMAND} --write")


def _validate_metadata(title: str | None, description: str | None, tags: list[str] | None) -> None:
    if title is not None:
        if not title.strip():
            raise ToolError("title cannot be empty.")
        if len(title) > MAX_TITLE:
            raise ToolError(f"title is {len(title)} characters; YouTube allows {MAX_TITLE}.")
        if "<" in title or ">" in title:
            raise ToolError("title cannot contain '<' or '>'.")
    if description is not None:
        if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
            raise ToolError(f"description exceeds YouTube's {MAX_DESCRIPTION_BYTES}-byte limit.")
        if "<" in description or ">" in description:
            raise ToolError("description cannot contain '<' or '>'.")
    if tags is not None:
        # YouTube counts tags with spaces as if quoted, plus separators.
        length = sum(len(tag) + (2 if " " in tag else 0) for tag in tags) + max(0, len(tags) - 1)
        if length > MAX_TAGS_CHARS:
            raise ToolError(f"tags total {length} characters; YouTube allows {MAX_TAGS_CHARS}.")


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(title="Update video details", annotations=EDIT)
    @tool_errors
    def update_video_details(
        video_id: Annotated[str, Field(description="The video to edit.")],
        title: Annotated[str | None, Field(description="New title (max 100 characters).")] = None,
        description: Annotated[
            str | None, Field(description="New full description (replaces the old one).")
        ] = None,
        tags: Annotated[
            list[str] | None, Field(description="New full tag list (replaces the old one).")
        ] = None,
        category_id: Annotated[
            str | None, Field(description="YouTube category ID, e.g. '22' (People & Blogs).")
        ] = None,
        privacy_status: Annotated[
            Literal["public", "unlisted", "private"] | None, Field(description="Change visibility.")
        ] = None,
        dry_run: Annotated[
            bool,
            Field(
                description="Preview the change without saving. Recommended first; show the user the diff."
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Edit a video's title, description, tags, category or visibility. Fields you omit are
        kept as they are. Defaults to dry_run=true: confirm the shown diff with the user, then call
        again with dry_run=false. Costs 50 quota units when saving."""
        _needs_write_scope(ctx)
        if all(v is None for v in (title, description, tags, category_id, privacy_status)):
            raise ToolError(
                "Nothing to change: pass at least one of title, description, tags, category_id, privacy_status."
            )
        _validate_metadata(title, description, tags)
        response = ctx.api.data("videos", "list", part="snippet,status", id=video_id)
        items = response.get("items") or []
        if not items:
            raise ToolError(f"Video {video_id} was not found (or is not owned by this channel).")
        item = items[0]
        old_snippet = item.get("snippet") or {}
        old_status = item.get("status") or {}
        snippet = {key: old_snippet[key] for key in _SNIPPET_FIELDS if key in old_snippet}
        changes: dict[str, dict[str, Any]] = {}
        for key, value in (
            ("title", title),
            ("description", description),
            ("tags", tags),
            ("categoryId", category_id),
        ):
            if value is not None and old_snippet.get(key) != value:
                changes[key] = {"before": old_snippet.get(key), "after": value}
                snippet[key] = value
        status = {key: old_status[key] for key in _STATUS_FIELDS if key in old_status}
        if privacy_status and old_status.get("privacyStatus") != privacy_status:
            changes["privacyStatus"] = {"before": old_status.get("privacyStatus"), "after": privacy_status}
            status["privacyStatus"] = privacy_status
            status.pop("publishAt", None)  # a scheduled publish time conflicts with an explicit change
        if not changes:
            return {"video_id": video_id, "changed": False, "message": "Already up to date; nothing to save."}
        if dry_run:
            return {"video_id": video_id, "dry_run": True, "changes": changes}
        parts = ["snippet"]
        body: dict[str, Any] = {"id": video_id, "snippet": snippet}
        if "privacyStatus" in changes:
            parts.append("status")
            body["status"] = status
        ctx.api.data("videos", "update", part=",".join(parts), body=body)
        return {"video_id": video_id, "saved": True, "changes": changes}

    @mcp.tool(title="Reply to comment", annotations=ADD)
    @tool_errors
    def reply_to_comment(
        comment_id: Annotated[str, Field(description="ID of the top-level comment to reply to.")],
        text: Annotated[
            str, Field(min_length=1, max_length=10000, description="Reply text, posted as the channel.")
        ],
    ) -> dict[str, Any]:
        """Post a public reply as the channel. Always show the user the exact text and get their
        approval first. Costs 50 quota units."""
        _needs_write_scope(ctx)
        response = ctx.api.data(
            "comments",
            "insert",
            part="snippet",
            body={"snippet": {"parentId": comment_id, "textOriginal": text}},
        )
        return {"posted": True, "reply_id": response.get("id"), "parent_id": comment_id}

    @mcp.tool(title="Moderate comments", annotations=EDIT)
    @tool_errors
    def moderate_comments(
        comment_ids: Annotated[
            list[str], Field(min_length=1, max_length=50, description="Up to 50 comment IDs.")
        ],
        status: Annotated[
            Literal["published", "heldForReview", "rejected"],
            Field(
                description="published = approve, heldForReview = hold, rejected = hide (cannot be undone)."
            ),
        ],
        ban_author: Annotated[
            bool, Field(description="With 'rejected': also hide future comments from these authors.")
        ] = False,
    ) -> dict[str, Any]:
        """Approve, hold or reject comments (like Studio's comment moderation). Confirm with the user
        before rejecting. Costs 50 quota units."""
        _needs_write_scope(ctx)
        if ban_author and status != "rejected":
            raise ToolError("ban_author can only be used with status='rejected'.")
        params: dict[str, Any] = {"id": ",".join(comment_ids), "moderationStatus": status}
        if ban_author:
            params["banAuthor"] = True
        ctx.api.data("comments", "setModerationStatus", **params)
        return {"updated": len(comment_ids), "status": status, "banned_authors": ban_author}
