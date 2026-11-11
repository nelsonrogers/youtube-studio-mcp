import json
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from tests.fakes import FakeYouTubeAPI
from youtube_studio_mcp.youtube import (
    error_from_http,
    get_my_channel,
    get_videos,
    guess_format,
    list_playlist_video_ids,
    parse_duration,
    summarize_video,
)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("PT1H2M3S", 3723),
        ("PT45S", 45),
        ("PT10M", 600),
        ("P1DT1S", 86401),
        ("P0D", 0),
        ("bogus", None),
        (None, None),
    ],
)
def test_parse_duration(value: str | None, seconds: int | None) -> None:
    assert parse_duration(value) == seconds


def test_guess_format() -> None:
    assert guess_format(45, None, {}) == "short"
    assert guess_format(150, None, {}) == "short_or_video"
    assert guess_format(600, None, {}) == "video"
    assert guess_format(600, {"actualStartTime": "x"}, {}) == "live"
    assert guess_format(None, None, {"liveBroadcastContent": "upcoming"}) == "live"
    assert guess_format(None, None, {}) == "unknown"


def http_error(status: int, error: dict[str, Any]) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), json.dumps({"error": error}).encode())


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (403, {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}, "quota"),
        (
            403,
            {
                "message": "YouTube Analytics API has not been used in project 123 before or it is disabled.",
                "details": [
                    {"reason": "SERVICE_DISABLED", "metadata": {"activationUrl": "https://console/x"}}
                ],
            },
            "https://console/x",
        ),
        (
            403,
            {"message": "scope", "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]},
            "tick every checkbox",
        ),
        (403, {"message": "x", "errors": [{"reason": "youtubeSignupRequired"}]}, "no YouTube channel"),
        (401, {"message": "Invalid Credentials"}, "sign in again"),
        (400, {"message": "The query is not supported."}, "combination of dimensions"),
        (404, {"message": "Video not found", "errors": [{"reason": "videoNotFound"}]}, "Not found"),
        (503, {"message": "Backend Error"}, "temporarily unavailable"),
    ],
)
def test_error_messages_are_actionable(status: int, error: dict[str, Any], expected: str) -> None:
    exc = error_from_http(http_error(status, error), api="YouTube Analytics API")
    assert exc.status == status
    assert expected in str(exc)


def test_error_without_json_body() -> None:
    exc = error_from_http(
        HttpError(httplib2.Response({"status": 500}), b"<html>oops</html>"), api="YouTube Data API"
    )
    assert "500" in str(exc)


def test_channel_and_videos_helpers() -> None:
    api = FakeYouTubeAPI(video_count=120)
    assert get_my_channel(api)["id"].startswith("UC")
    ids = list_playlist_video_ids(api, "UU", 105)
    assert len(ids) == 105 and len(set(ids)) == 105
    pages = [c for c in api.calls if c[0] == "data.playlistItems.list"]
    assert len(pages) == 3
    videos = get_videos(api, ids[:75])
    assert [v["id"] for v in videos] == ids[:75]  # order preserved across 50-ID batches


def test_summarize_video() -> None:
    api = FakeYouTubeAPI()
    video = summarize_video(api.videos[0])
    assert video["video_id"] == "vid00000000"
    assert video["format"] == "video"
    assert video["thumbnail_url"].startswith("https://")
    assert video["engagement_rate_pct"] == pytest.approx(
        (video["likes"] + video["comments"]) / video["views"] * 100, abs=0.01
    )
