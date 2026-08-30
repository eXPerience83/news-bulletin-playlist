from __future__ import annotations

import base64
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPMessage
from pathlib import Path

import pytest

from news_bulletin_playlist.first_release import (
    DEFAULT_CONFIG_FILENAME,
    FirstReleaseProvisioningResult,
)
from news_bulletin_playlist.lan_admin import LanAdminHandler, LanAdminSecurity
from news_bulletin_playlist.spotify.auth import AuthorizationState

_PASSWORD = "long-enough-admin-password"
_PLAYLIST_ID = "1234567890123456789012"
_ACCESS_TOKEN = "access-token-sentinel"


class _FakeAuth:
    def __init__(self, state: AuthorizationState = AuthorizationState.CONNECTED) -> None:
        self.state = state
        self.access_token_calls = 0

    def authorization_state(self) -> AuthorizationState:
        return self.state

    def get_access_token(self) -> str:
        self.access_token_calls += 1
        return _ACCESS_TOKEN


@dataclass(frozen=True, slots=True)
class _CapturedResponse:
    status: HTTPStatus
    payload: bytes
    content_type: str
    extra_headers: dict[str, str]


class _HandlerHarness(LanAdminHandler):
    def __init__(
        self,
        *,
        data_dir: Path,
        security: LanAdminSecurity,
        auth: _FakeAuth,
        path: str,
        form: dict[str, list[str]] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.admin_security = security
        self.spotify_auth = auth  # type: ignore[assignment]
        self.path = path
        self.client_address = ("127.0.0.1", 12345)
        self.headers = HTTPMessage()
        self.headers["Authorization"] = _basic_header(_PASSWORD)
        self._form = {} if form is None else form
        self.response: _CapturedResponse | None = None

    def _read_form(self) -> dict[str, list[str]]:
        return self._form

    def _reply(
        self,
        status: HTTPStatus,
        payload: bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.response = _CapturedResponse(
            status=status,
            payload=payload,
            content_type=content_type,
            extra_headers={} if extra_headers is None else dict(extra_headers),
        )


def _basic_header(password: str) -> str:
    encoded = base64.b64encode(f"admin:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


def _response(handler: _HandlerHarness) -> _CapturedResponse:
    assert handler.response is not None
    return handler.response


def test_admin_shows_provision_action_only_when_connected_and_config_missing(
    tmp_path: Path,
) -> None:
    security = LanAdminSecurity(_PASSWORD)
    connected = _HandlerHarness(
        data_dir=tmp_path,
        security=security,
        auth=_FakeAuth(),
        path="/admin/",
    )
    connected.do_GET()
    assert _response(connected).status == HTTPStatus.OK
    assert "Provision first playlist" in _response(connected).payload.decode()

    disconnected = _HandlerHarness(
        data_dir=tmp_path,
        security=security,
        auth=_FakeAuth(AuthorizationState.DISCONNECTED),
        path="/admin/",
    )
    disconnected.do_GET()
    assert "Provision first playlist" not in _response(disconnected).payload.decode()

    (tmp_path / DEFAULT_CONFIG_FILENAME).write_text("existing", encoding="utf-8")
    configured = _HandlerHarness(
        data_dir=tmp_path,
        security=security,
        auth=_FakeAuth(),
        path="/admin/",
    )
    configured.do_GET()
    body = _response(configured).payload.decode()
    assert "Provision first playlist" not in body
    assert "Engine configuration: <strong>Present</strong>" in body


def test_admin_provision_success_uses_token_once_and_requests_clean_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    security = LanAdminSecurity(_PASSWORD)
    auth = _FakeAuth()
    provision_calls: list[tuple[Path, str]] = []
    restart_calls = 0

    def fake_provision(data_dir: Path, access_token: str) -> FirstReleaseProvisioningResult:
        provision_calls.append((data_dir, access_token))
        (data_dir / DEFAULT_CONFIG_FILENAME).write_text("configured", encoding="utf-8")
        return FirstReleaseProvisioningResult(
            playlist_id=_PLAYLIST_ID,
            config_path=data_dir / DEFAULT_CONFIG_FILENAME,
        )

    def restart() -> None:
        nonlocal restart_calls
        restart_calls += 1

    monkeypatch.setattr("news_bulletin_playlist.lan_admin.provision_first_release", fake_provision)
    monkeypatch.setattr(LanAdminHandler, "runtime_restart", restart)

    handler = _HandlerHarness(
        data_dir=tmp_path,
        security=security,
        auth=auth,
        path="/admin/provision-first-playlist",
        form={"csrf_token": [security.issue_csrf_token()]},
    )
    handler.do_POST()

    response = _response(handler)
    body = response.payload.decode()
    assert response.status == HTTPStatus.CREATED
    assert provision_calls == [(tmp_path, _ACCESS_TOKEN)]
    assert auth.access_token_calls == 1
    assert restart_calls == 1
    assert _PLAYLIST_ID in body
    assert "restarting automatically" in body

    captured = capsys.readouterr()
    assert _ACCESS_TOKEN not in body
    assert _ACCESS_TOKEN not in captured.out
    assert _ACCESS_TOKEN not in captured.err


def test_admin_retry_with_existing_config_never_requests_token_or_spotify_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / DEFAULT_CONFIG_FILENAME).write_text("existing", encoding="utf-8")
    security = LanAdminSecurity(_PASSWORD)
    auth = _FakeAuth()
    provision_called = False
    restart_called = False

    def fail_if_provisioned(data_dir: Path, access_token: str) -> FirstReleaseProvisioningResult:
        nonlocal provision_called
        del data_dir, access_token
        provision_called = True
        raise AssertionError("Spotify provisioning must not run when config already exists")

    def restart() -> None:
        nonlocal restart_called
        restart_called = True

    monkeypatch.setattr("news_bulletin_playlist.lan_admin.provision_first_release", fail_if_provisioned)
    monkeypatch.setattr(LanAdminHandler, "runtime_restart", restart)

    handler = _HandlerHarness(
        data_dir=tmp_path,
        security=security,
        auth=auth,
        path="/admin/provision-first-playlist",
        form={"csrf_token": [security.issue_csrf_token()]},
    )
    handler.do_POST()

    assert _response(handler).status == HTTPStatus.CONFLICT
    assert not provision_called
    assert auth.access_token_calls == 0
    assert not restart_called


def test_admin_provision_endpoint_rejects_replayed_csrf_without_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = LanAdminSecurity(_PASSWORD)
    auth = _FakeAuth()
    token = security.issue_csrf_token()
    assert security.consume_csrf_token(token)
    provision_called = False

    def fail_if_provisioned(data_dir: Path, access_token: str) -> FirstReleaseProvisioningResult:
        nonlocal provision_called
        del data_dir, access_token
        provision_called = True
        raise AssertionError("replayed CSRF must fail before provisioning")

    monkeypatch.setattr("news_bulletin_playlist.lan_admin.provision_first_release", fail_if_provisioned)

    handler = _HandlerHarness(
        data_dir=tmp_path,
        security=security,
        auth=auth,
        path="/admin/provision-first-playlist",
        form={"csrf_token": [token]},
    )
    handler.do_POST()

    assert _response(handler).status == HTTPStatus.FORBIDDEN
    assert not provision_called
    assert auth.access_token_calls == 0
