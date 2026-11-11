import json
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from google.oauth2.credentials import Credentials

from youtube_studio_mcp.auth import (
    ANALYTICS_MONETARY_READONLY,
    ANALYTICS_READONLY,
    YOUTUBE_MANAGE,
    YOUTUBE_READONLY,
    AuthError,
    LoopbackOAuthFlow,
    NotAuthenticatedError,
    TokenStore,
    install_client_secret,
    load_client_config,
    load_credentials,
    requested_scopes,
)
from youtube_studio_mcp.config import Settings

CLIENT = {
    "installed": {
        "client_id": "id.apps.googleusercontent.com",
        "client_secret": "secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


def test_requested_scopes() -> None:
    assert requested_scopes() == [YOUTUBE_READONLY, ANALYTICS_READONLY, ANALYTICS_MONETARY_READONLY]
    assert requested_scopes(revenue=False, write=True) == [
        YOUTUBE_READONLY,
        ANALYTICS_READONLY,
        YOUTUBE_MANAGE,
    ]


def test_client_config_from_env_vars(settings: Settings) -> None:
    configured = Settings(config_dir=settings.config_dir, client_id="abc", client_secret="xyz")
    assert load_client_config(configured)["installed"]["client_id"] == "abc"
    with pytest.raises(AuthError, match="both"):
        load_client_config(Settings(config_dir=settings.config_dir, client_id="abc"))


def test_client_config_missing_file_explains_setup(settings: Settings) -> None:
    with pytest.raises(AuthError, match="Desktop app"):
        load_client_config(settings)


def test_client_config_rejects_web_clients(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "web.json"
    path.write_text(json.dumps({"web": {"client_id": "x"}}))
    with pytest.raises(AuthError, match="Web application"):
        load_client_config(Settings(config_dir=settings.config_dir, client_secret_file=path))
    path.write_text("not json")
    with pytest.raises(AuthError, match="Could not read"):
        load_client_config(Settings(config_dir=settings.config_dir, client_secret_file=path))


def test_install_client_secret_copies_privately(settings: Settings, tmp_path: Path) -> None:
    source = tmp_path / "client_secret_123.json"
    source.write_text(json.dumps(CLIENT))
    destination = install_client_secret(source, settings)
    assert destination == settings.default_client_secret_file
    assert load_client_config(settings) == CLIENT
    if sys.platform != "win32":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def make_credentials(**overrides: Any) -> Credentials:
    info = {
        "token": "access",
        "refresh_token": "refresh",
        "client_id": "id",
        "client_secret": "secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": "2999-01-01T00:00:00Z",
        **overrides,
    }
    return Credentials.from_authorized_user_info(info)


def test_token_store_roundtrip(settings: Settings) -> None:
    store = TokenStore(settings.token_file)
    assert store.load() is None
    store.save(make_credentials(), [YOUTUBE_READONLY, ANALYTICS_READONLY])
    credentials, granted = store.load() or (None, None)
    assert credentials is not None and credentials.refresh_token == "refresh"
    assert granted == sorted([YOUTUBE_READONLY, ANALYTICS_READONLY])
    if sys.platform != "win32":
        assert stat.S_IMODE(settings.token_file.stat().st_mode) == 0o600
    assert store.delete() is True
    assert store.delete() is False


def test_corrupt_token_is_reported(settings: Settings) -> None:
    settings.token_file.parent.mkdir(parents=True)
    settings.token_file.write_text("{}")
    with pytest.raises(AuthError, match="unreadable"):
        TokenStore(settings.token_file).load()


def test_load_credentials_when_signed_out(settings: Settings) -> None:
    with pytest.raises(NotAuthenticatedError, match="authenticate"):
        load_credentials(settings)


def test_load_credentials_valid_token_needs_no_network(settings: Settings) -> None:
    TokenStore(settings.token_file).save(make_credentials(), [YOUTUBE_READONLY])
    credentials, granted = load_credentials(settings)
    assert credentials.token == "access"
    assert granted == [YOUTUBE_READONLY]


# --------------------------------------------------------------------------- loopback flow


def start_flow(
    saved: list[Any], monkeypatch: pytest.MonkeyPatch, *, granted: str | None = None
) -> LoopbackOAuthFlow:
    flow = LoopbackOAuthFlow(
        CLIENT, requested_scopes(), on_success=lambda c, g: saved.append((c, g)), timeout_seconds=10
    )

    def fake_fetch_token(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["code"] == "the-code"
        token = {
            "access_token": "at",
            "refresh_token": "rt",
            "token_type": "Bearer",
            "expires_in": 3600,
            "expires_at": time.time() + 3600,
            "scope": granted if granted is not None else " ".join(requested_scopes()),
        }
        flow._flow.oauth2session.token = token
        return token

    monkeypatch.setattr(flow._flow, "fetch_token", fake_fetch_token)
    url = flow.start(open_browser=False)
    assert "access_type=offline" in url and "code_challenge=" in url  # refresh token + PKCE
    assert flow.redirect_uri.startswith("http://127.0.0.1:")
    return flow


def callback(flow: LoopbackOAuthFlow, **params: str) -> tuple[int, str]:
    url = f"{flow.redirect_uri}?{urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def state_of(flow: LoopbackOAuthFlow) -> str:
    return parse_qs(urlparse(flow.auth_url).query)["state"][0]


def test_flow_success_saves_token(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[Any] = []
    flow = start_flow(saved, monkeypatch)
    status, body = callback(flow, code="the-code", state=state_of(flow))
    assert status == 200 and "signed in" in body
    assert flow.wait(timeout=5) is True
    assert flow.status == "succeeded" and flow.missing_scopes == []
    assert saved[0][0].refresh_token == "rt"


def test_flow_reports_missing_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[Any] = []
    flow = start_flow(saved, monkeypatch, granted=YOUTUBE_READONLY)
    callback(flow, code="the-code", state=state_of(flow))
    flow.wait(timeout=5)
    assert flow.missing_scopes == [ANALYTICS_READONLY, ANALYTICS_MONETARY_READONLY]


def test_flow_ignores_wrong_state_then_accepts_real_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[Any] = []
    flow = start_flow(saved, monkeypatch)
    status, _ = callback(flow, code="the-code", state="forged")
    assert status == 400 and not flow.finished
    callback(flow, code="the-code", state=state_of(flow))
    assert flow.wait(timeout=5) is True


def test_flow_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[Any] = []
    flow = start_flow(saved, monkeypatch)
    status, body = callback(flow, error="access_denied", state=state_of(flow))
    assert status == 400 and "cancelled" in body.lower()
    assert flow.wait(timeout=5) is False
    assert flow.status == "failed" and saved == []


def test_flow_ignores_unrelated_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    flow = start_flow([], monkeypatch)
    try:
        urllib.request.urlopen(flow.redirect_uri + "favicon.ico", timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    flow.cancel()
    assert flow.status == "cancelled"
