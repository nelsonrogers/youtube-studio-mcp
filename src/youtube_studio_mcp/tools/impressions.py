"""Thumbnail impressions and click-through rate via the YouTube Reporting API (beta).

The Analytics API has no impressions metric. The Reporting API publishes daily
``channel_reach_basic_a1`` CSV reports once a reporting job exists for the channel:
the first files appear ~48 hours after the job is created, with a backfill of the
previous 30 days. Downloaded files are cached under the config directory.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from youtube_studio_mcp.context import READ_ONLY, AppContext, EndDateArg, PeriodArg, StartDateArg, tool_errors
from youtube_studio_mcp.periods import PACIFIC, DateRange
from youtube_studio_mcp.tools.analytics import video_titles

REACH_REPORT_TYPE = "channel_reach_basic_a1"
JOB_NAME = "youtube-studio-mcp impressions"


def _paginate(ctx: AppContext, resource: str, key: str, **params: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(50):
        call = dict(params)
        if page_token:
            call["pageToken"] = page_token
        response = ctx.api.reporting(resource, "list", **call)
        items.extend(response.get(key) or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return items


def find_reach_job(ctx: AppContext) -> dict[str, Any] | None:
    for job in _paginate(ctx, "jobs", "jobs"):
        if job.get("reportTypeId") == REACH_REPORT_TYPE:
            return job
    return None


def _report_day(report: dict[str, Any]) -> date:
    start = datetime.fromisoformat(str(report["startTime"]).replace("Z", "+00:00"))
    return start.astimezone(PACIFIC).date()


def _cache_dir(ctx: AppContext, job_id: str) -> Path:
    return ctx.settings.config_dir / "reports" / ctx.settings.profile / job_id


def load_reach_rows(
    ctx: AppContext, job_id: str, date_range: DateRange
) -> tuple[list[dict[str, str]], list[date]]:
    """Download (or read cached) daily reach reports covering ``date_range``.

    Returns the CSV rows and the list of days in range that have no report yet.
    """
    reports = _paginate(ctx, "jobs.reports", "reports", jobId=job_id)
    latest_per_day: dict[date, dict[str, Any]] = {}
    for report in reports:
        if not report.get("startTime") or not report.get("downloadUrl"):
            continue
        day = _report_day(report)
        if not date_range.start <= day <= date_range.end:
            continue
        current = latest_per_day.get(day)
        if current is None or str(report.get("createTime", "")) > str(current.get("createTime", "")):
            latest_per_day[day] = report  # backfills supersede earlier files for the same day

    cache = _cache_dir(ctx, job_id)
    rows: list[dict[str, str]] = []
    for _day, report in sorted(latest_per_day.items()):
        path = cache / f"{report['id']}.csv"
        if path.is_file():
            text = path.read_text(encoding="utf-8")
        else:
            text = ctx.api.download(str(report["downloadUrl"])).decode("utf-8")
            cache.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        rows.extend(csv.DictReader(io.StringIO(text)))

    missing = []
    day = date_range.start
    while day <= date_range.end:
        if day not in latest_per_day:
            missing.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return rows, missing


def _parse_reach(rows: list[dict[str, str]]) -> tuple[list[tuple[str, str, float, float]], str]:
    """Return ``(date, video_id, impressions, clicks)`` tuples and the detected CTR scale."""
    parsed = []
    for row in rows:
        try:
            impressions = float(row.get("video_thumbnail_impressions") or 0)
            ctr = float(row.get("video_thumbnail_impressions_ctr") or 0)
        except ValueError:
            continue
        parsed.append((row.get("date", ""), row.get("video_id", ""), impressions, ctr))
    # Google documents the CTR as a "percentage" without stating the scale; infer it.
    scale = "percent_0_100" if any(ctr > 1 for *_, ctr in parsed) else "fraction_0_1"
    divisor = 100.0 if scale == "percent_0_100" else 1.0
    return [(day, video, imp, imp * ctr / divisor) for day, video, imp, ctr in parsed], scale


@dataclass
class ReachData:
    """Impressions for a date range, keyed by video: ``{video_id: (impressions, clicks)}``."""

    by_video: dict[str, tuple[float, float]]
    ctr_scale: str
    missing_days: list[date]
    job: dict[str, Any]

    @property
    def complete(self) -> bool:
        return not self.missing_days

    def totals(self, video_ids: set[str] | None = None) -> tuple[int, float | None]:
        """``(impressions, ctr_pct)`` over all videos, or only ``video_ids``."""
        impressions = clicks = 0.0
        for video_id, (imp, clk) in self.by_video.items():
            if video_ids is None or video_id in video_ids:
                impressions += imp
                clicks += clk
        return round(impressions), (round(clicks / impressions * 100, 2) if impressions else None)


def load_reach(ctx: AppContext, date_range: DateRange) -> ReachData | None:
    """Impressions for ``date_range``, or None when no reach job exists yet."""
    job = find_reach_job(ctx)
    if not job:
        return None
    rows, missing = load_reach_rows(ctx, str(job["id"]), date_range)
    parsed, scale = _parse_reach(rows)
    by_video: dict[str, tuple[float, float]] = {}
    for _day, video_id, impressions, clicks in parsed:
        imp, clk = by_video.get(video_id, (0.0, 0.0))
        by_video[video_id] = (imp + impressions, clk + clicks)
    return ReachData(by_video=by_video, ctr_scale=scale, missing_days=missing, job=job)


def aggregate_reach(
    rows: list[dict[str, str]], group_by: Literal["day", "video", "total"]
) -> tuple[list[dict[str, Any]], str]:
    """Sum impressions and derive CTR (weighted by impressions). Returns (rows, ctr_scale)."""
    parsed, scale = _parse_reach(rows)

    buckets: dict[str, list[float]] = {}
    for day, video_id, impressions, clicks in parsed:
        key = {"day": day, "video": video_id, "total": "total"}[group_by]
        bucket = buckets.setdefault(key, [0.0, 0.0])
        bucket[0] += impressions
        bucket[1] += clicks

    out: list[dict[str, Any]] = []
    for key, (impressions, clicks) in buckets.items():
        entry: dict[str, Any] = {
            "impressions": round(impressions),
            "ctr_pct": round(clicks / impressions * 100, 2) if impressions else None,
            "estimated_clicks": round(clicks),
        }
        if group_by == "day":
            entry = {"date": f"{key[:4]}-{key[4:6]}-{key[6:8]}" if len(key) == 8 else key, **entry}
        elif group_by == "video":
            entry = {"video_id": key, **entry}
        out.append(entry)
    if group_by == "day":
        out.sort(key=lambda r: str(r["date"]))
    else:
        out.sort(key=lambda r: int(r["impressions"]), reverse=True)
    return out, scale


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(
        title="Enable impressions reports",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    @tool_errors
    def enable_impressions_reports() -> dict[str, Any]:
        """One-time setup for thumbnail impressions & click-through rate (CTR), which the Analytics
        API does not provide. Creates a YouTube Reporting API job for the channel (safe to call
        again). YouTube then publishes daily reports: the first ones ~48 hours later, plus 30 days
        of history. Use get_impressions afterwards."""
        job = find_reach_job(ctx)
        if job:
            return {
                "status": "already_enabled",
                "job_id": job.get("id"),
                "created_at": job.get("createTime"),
                "next_step": "Call get_impressions.",
            }
        available = {item.get("id") for item in _paginate(ctx, "reportTypes", "reportTypes")}
        if REACH_REPORT_TYPE not in available:
            raise ToolError(
                "YouTube does not offer reach (impressions) reports for this channel through the "
                "Reporting API yet."
            )
        job = ctx.api.reporting("jobs", "create", body={"reportTypeId": REACH_REPORT_TYPE, "name": JOB_NAME})
        return {
            "status": "enabled",
            "job_id": job.get("id"),
            "created_at": job.get("createTime"),
            "next_step": (
                "YouTube needs up to ~48 hours to publish the first daily reports (including the previous "
                "30 days). Call get_impressions after that."
            ),
        }

    @mcp.tool(title="Impressions & CTR (beta)", annotations=READ_ONLY)
    @tool_errors
    def get_impressions(
        group_by: Annotated[
            Literal["day", "video", "total"],
            Field(description="Daily trend, per-video ranking, or one total for the period."),
        ] = "total",
        period: PeriodArg = "last_28_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows for group_by='video'.")] = 25,
    ) -> dict[str, Any]:
        """Thumbnail impressions and impressions click-through rate (CTR) from the YouTube
        Reporting API, by day, by video, or in total. Requires enable_impressions_reports to have
        been run at least ~2 days earlier; only days YouTube has published are included."""
        job = find_reach_job(ctx)
        if not job:
            raise ToolError(
                "Impressions reports are not enabled yet. Call enable_impressions_reports once; data "
                "becomes available about 48 hours later."
            )
        date_range = ctx.resolve(period, start_date, end_date)
        rows, missing = load_reach_rows(ctx, str(job["id"]), date_range)
        aggregated, scale = aggregate_reach(rows, group_by)
        if group_by == "video":
            aggregated = aggregated[:limit]
            titles = video_titles(ctx, [str(r["video_id"]) for r in aggregated if r.get("video_id")])
            for row in aggregated:
                row["title"] = (titles.get(str(row["video_id"])) or {}).get("title")
        notes = [
            "Impressions count thumbnails shown for 1+ second with 50%+ visible on YouTube "
            "(not external sites). ctr_pct is impressions-weighted.",
            f"CTR scale in the source files was detected as {scale}.",
        ]
        if missing:
            notes.append(
                f"{len(missing)} day(s) in the range have no report yet (first: {missing[0].isoformat()}, "
                f"last: {missing[-1].isoformat()}). Reports start ~30 days before the job was created "
                f"({job.get('createTime')}) and lag ~2 days."
            )
        return {
            "date_range": date_range.to_dict(),
            "days_with_data": date_range.days - len(missing),
            "group_by": group_by,
            "rows": aggregated,
            "notes": notes,
        }
