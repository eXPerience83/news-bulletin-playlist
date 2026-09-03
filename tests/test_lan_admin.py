from __future__ import annotations

import base64
import html
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.client import HTTPMessage
from pathlib import Path

import pytest

from news_bulletin_playlist.lan_admin import (
    LAN_SPOTIFY_REDIRECT_URI,
    LanAdminHandler,
    LanAdminSecurity,
    build_engine_runtime_auth,
    parse_lan_callback_url,
)
from news_bulletin_playlist.spotify.auth import (
    PRODUCTION_SCOPES,
    AuthorizationState,
    SpotifyAuthService,
    SpotifyCredentialStore,
    TokenResponse,
)


class _FakeTransport:
    def __init__(self) -> None:
        self.exchange_calls: list[dict[str, str]] = []
        self.refresh_calls: list[dict[str, str]] = []

    def exchange_code(
        self,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> TokenResponse:
        self.exchange_calls.append(
            {
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "verifier": verifier,
            }
        )
        return TokenResponse(
            access_token="access-token-sentinel",
            expires_in=3600,
            granted_scopes=PRODUCTION_SCOPES,
            refresh_token="refresh-token-sentinel",
        )

    def refresh_token(self, *, client_id: str, refresh_token: str) -> TokenResponse:
        self.refresh_calls.append({"client_id": client_id, "refresh_token": refresh_token})
        return TokenResponse(
            access_token="refreshed-access-token",
            expires_in=3600,
            granted_scopes=PRODUCTION_SCOPES,
            refresh_token=None,
        )


class _NeverExchangeTransport:
    def __init__(self) -> None:
        self.exchange_count = 0

    def exchange_code(
        self,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> TokenResponse:
        del client_id, code, redirect_uri, verifier
        self.exchange_count += 1
        raise AssertionError("expired callback must not reach token exchange")

    def refresh_token(self, *, client_id: str, refresh_token: str) -> TokenResponse:
        del client_id, refresh_token
        raise AssertionError("refresh is not expected")


@dataclass(frozen=True, slots=True)
class _CapturedResponse:
    status: HTTPStatus
    payload: bytes
    content_type: str
    extra_headers: dict[str, str]


class _HandlerHarness(LanAdminHandler):
    """Exercise handler methods with in-memory request/response state only."""

    def __init__(
        self,
        *,
        security: LanAdminSecurity,
        auth: SpotifyAuthService | None,
        path: str,
        form: dict[str, list[str]] | None = None,
        password: str | None = "long-enough-admin-password",
        client_ip: str = "127.0.0.1",
    ) -> None:
        self.admin_security = security
        self.spotify_auth = auth
        self.path = path
        self.client_address = (client_ip, 12345)
        self.headers = HTTPMessage()
        if password is not None:
            self.headers["Authorization"] = _basic_header(password)
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


def _body(handler: _HandlerHarness) -> str:
    return _response(handler).payload.decode()


def _callback_url(state: str, code: str) -> str:
    return LAN_SPOTIFY_REDIRECT_URI + "?" + urllib.parse.urlencode({"state": state, "code": code})


def test_lan_mode_configuration_is_explicit_and_conflicts_fail_closed(tmp_path: Path) -> None:
    password = "long-enough-admin-password"
    admin, auth = build_engine_runtime_auth(
        tmp_path,
        environ={
            "NEWS_PLAYLIST_ADMIN_MODE": "lan",
            "NEWS_PLAYLIST_ADMIN_PASSWORD": password,
            "SPOTIFY_CLIENT_ID": "client-id",
        },
    )
    assert isinstance(admin, LanAdminSecurity)
    assert auth is not None
    assert auth.redirect_uri == LAN_SPOTIFY_REDIRECT_URI

    for conflicting in (
        {"NEWS_PLAYLIST_EXTERNAL_URL": "https://news.example.test"},
        {"NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS": "10.0.0.2/32"},
    ):
        env = {
            "NEWS_PLAYLIST_ADMIN_MODE": "lan",
            "NEWS_PLAYLIST_ADMIN_PASSWORD": password,
            "SPOTIFY_CLIENT_ID": "client-id",
            **conflicting,
        }
        with pytest.raises(RuntimeError):
            build_engine_runtime_auth(tmp_path, environ=env)


def test_lan_transport_accepts_private_direct_client_and_rejects_forwarded_headers() -> None:
    security = LanAdminSecurity("long-enough-admin-password")
    assert security.is_secure_transport("192.168.1.50", None)
    assert security.is_secure_transport("127.0.0.1", None)
    assert not security.is_secure_transport("8.8.8.8", None)
    assert not security.is_secure_transport("192.168.1.50", ["https"])


def test_pasted_callback_url_must_match_exact_loopback_redirect_without_secret_echo() -> None:
    query = parse_lan_callback_url("http://127.0.0.1:8787/admin/spotify/callback?state=s&code=c")
    assert query == "state=s&code=c"

    code = "authorization-code-sentinel"
    invalid = f"http://localhost:8787/admin/spotify/callback?state=s&code={code}"
    with pytest.raises(ValueError) as raised:
        parse_lan_callback_url(invalid)
    assert code not in str(raised.value)
    assert invalid not in str(raised.value)

    for candidate in (
        "http://127.0.0.1:8788/admin/spotify/callback?state=s&code=c",
        "http://127.0.0.1:8787/callback?state=s&code=c",
        "https://127.0.0.1:8787/admin/spotify/callback?state=s&code=c",
        "http://127.0.0.1:8787/admin/spotify/callback",
    ):
        with pytest.raises(ValueError):
            parse_lan_callback_url(candidate)


def test_lan_mode_closes_direct_get_callback_route() -> None:
    security = LanAdminSecurity("long-enough-admin-password")
    handler = _HandlerHarness(
        security=security,
        auth=None,
        path="/admin/spotify/callback?state=s&code=c",
        password=None,
    )
    handler.do_GET()
    response = _response(handler)
    assert response.status == HTTPStatus.NOT_FOUND
    assert response.payload == b"Not found"
    assert "WWW-Authenticate" not in response.extra_headers


def test_lan_admin_connect_paste_persists_refresh_and_survives_restart(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    password = "long-enough-admin-password"
    transport = _FakeTransport()
    store = SpotifyCredentialStore(tmp_path / "spotify-auth.json")
    service = SpotifyAuthService(
        client_id="client-id",
        redirect_uri=LAN_SPOTIFY_REDIRECT_URI,
        store=store,
        transport=transport,
    )
    security = LanAdminSecurity(password)

    admin = _HandlerHarness(security=security, auth=service, path="/admin/", password=password)
    admin.do_GET()
    assert _response(admin).status == HTTPStatus.OK
    admin_body = _body(admin)
    assert "LAN development mode" in admin_body
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', admin_body)
    assert csrf is not None

    connect = _HandlerHarness(
        security=security,
        auth=service,
        path="/admin/spotify/connect",
        form={"csrf_token": [csrf.group(1)]},
        password=password,
    )
    connect.do_POST()
    assert _response(connect).status == HTTPStatus.OK
    connect_body = _body(connect)
    authorize_match = re.search(r'href="([^"]+)" target="_blank"', connect_body)
    callback_csrf = re.search(r'name="csrf_token" value="([^"]+)"', connect_body)
    assert authorize_match is not None
    assert callback_csrf is not None
    authorize_url = html.unescape(authorize_match.group(1))
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)
    assert params["redirect_uri"] == [LAN_SPOTIFY_REDIRECT_URI]
    state = params["state"][0]

    code = "authorization-code-sentinel"
    callback_url = _callback_url(state, code)
    callback = _HandlerHarness(
        security=security,
        auth=service,
        path="/admin/spotify/manual-callback",
        form={"csrf_token": [callback_csrf.group(1)], "callback_url": [callback_url]},
        password=password,
    )
    callback.do_POST()
    callback_response = _response(callback)
    assert callback_response.status == HTTPStatus.SEE_OTHER
    assert callback_response.extra_headers["Location"] == "/admin/"
    assert service.authorization_state() is AuthorizationState.CONNECTED
    assert len(transport.exchange_calls) == 1

    verifier = transport.exchange_calls[0]["verifier"]
    raw = store.path.read_text()
    assert "refresh-token-sentinel" in raw
    for transient_secret in ("access-token-sentinel", code, verifier, callback_url):
        assert transient_secret not in raw

    restarted_transport = _FakeTransport()
    restarted = SpotifyAuthService(
        client_id="client-id",
        redirect_uri=LAN_SPOTIFY_REDIRECT_URI,
        store=store,
        transport=restarted_transport,
    )
    assert restarted.authorization_state() is AuthorizationState.CONNECTED
    assert restarted.get_access_token() == "refreshed-access-token"
    assert restarted_transport.refresh_calls == [
        {"client_id": "client-id", "refresh_token": "refresh-token-sentinel"}
    ]

    captured = capsys.readouterr()
    rendered = admin_body + connect_body + callback_response.payload.decode()
    for secret in (
        callback_url,
        code,
        verifier,
        "access-token-sentinel",
        "refresh-token-sentinel",
    ):
        assert secret not in rendered
        assert secret not in captured.out
        assert secret not in captured.err


def test_lan_callback_replay_fails_without_second_exchange_or_secret_echo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    password = "long-enough-admin-password"
    transport = _FakeTransport()
    store = SpotifyCredentialStore(tmp_path / "spotify-auth.json")
    service = SpotifyAuthService(
        client_id="client-id",
        redirect_uri=LAN_SPOTIFY_REDIRECT_URI,
        store=store,
        transport=transport,
    )
    security = LanAdminSecurity(password)
    authorize_url = service.start_authorization()
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)["state"][0]
    code = "authorization-code-sentinel"
    callback_url = _callback_url(state, code)

    first = _HandlerHarness(
        security=security,
        auth=service,
        path="/admin/spotify/manual-callback",
        form={"csrf_token": [security.issue_csrf_token()], "callback_url": [callback_url]},
        password=password,
    )
    first.do_POST()
    assert _response(first).status == HTTPStatus.SEE_OTHER
    assert len(transport.exchange_calls) == 1
    verifier = transport.exchange_calls[0]["verifier"]

    replay = _HandlerHarness(
        security=security,
        auth=service,
        path="/admin/spotify/manual-callback",
        form={"csrf_token": [security.issue_csrf_token()], "callback_url": [callback_url]},
        password=password,
    )
    replay.do_POST()
    replay_response = _response(replay)
    assert replay_response.status == HTTPStatus.BAD_REQUEST
    assert len(transport.exchange_calls) == 1

    captured = capsys.readouterr()
    user_output = replay_response.payload.decode()
    for secret in (
        callback_url,
        code,
        verifier,
        "access-token-sentinel",
        "refresh-token-sentinel",
    ):
        assert secret not in user_output
        assert secret not in captured.out
        assert secret not in captured.err


def test_expired_lan_callback_form_fails_closed_before_exchange(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    password = "long-enough-admin-password"
    transport = _NeverExchangeTransport()
    service = SpotifyAuthService(
        client_id="client-id",
        redirect_uri=LAN_SPOTIFY_REDIRECT_URI,
        store=SpotifyCredentialStore(tmp_path / "spotify-auth.json"),
        transport=transport,
    )
    authorize_url = service.start_authorization(now=datetime(2000, 1, 1, tzinfo=UTC))
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)["state"][0]
    code = "expired-code-sentinel"
    callback_url = _callback_url(state, code)
    security = LanAdminSecurity(password)
    handler = _HandlerHarness(
        security=security,
        auth=service,
        path="/admin/spotify/manual-callback",
        form={"csrf_token": [security.issue_csrf_token()], "callback_url": [callback_url]},
        password=password,
    )
    handler.do_POST()

    response = _response(handler)
    assert response.status == HTTPStatus.BAD_REQUEST
    body = response.payload.decode()
    assert "invalid or expired" in body
    assert transport.exchange_count == 0
    captured = capsys.readouterr()
    for secret in (callback_url, code):
        assert secret not in body
        assert secret not in captured.out
        assert secret not in captured.err
