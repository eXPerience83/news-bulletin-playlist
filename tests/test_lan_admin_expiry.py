from __future__ import annotations

import base64
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path

import pytest

from news_bulletin_playlist.lan_admin import (
    LAN_SPOTIFY_REDIRECT_URI,
    LanAdminHandler,
    LanAdminSecurity,
)
from news_bulletin_playlist.spotify.auth import (
    SpotifyAuthService,
    SpotifyCredentialStore,
    TokenResponse,
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


class _Handler(LanAdminHandler):
    pass


def _basic_header(password: str) -> str:
    encoded = base64.b64encode(f"admin:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


def test_expired_lan_callback_form_fails_closed_before_exchange(tmp_path: Path) -> None:
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
    callback_url = LAN_SPOTIFY_REDIRECT_URI + "?" + urllib.parse.urlencode(
        {"state": state, "code": "expired-code-sentinel"}
    )

    security = LanAdminSecurity(password)
    _Handler.data_dir = tmp_path
    _Handler.admin_security = security
    _Handler.spotify_auth = service
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/admin/spotify/manual-callback",
        data=urllib.parse.urlencode(
            {"csrf_token": security.issue_csrf_token(), "callback_url": callback_url}
        ).encode(),
        headers={
            "Authorization": _basic_header(password),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        assert raised.value.code == HTTPStatus.BAD_REQUEST
        body = raised.value.read().decode()
        assert "invalid or expired" in body
        assert callback_url not in body
        assert "expired-code-sentinel" not in body
        assert transport.exchange_count == 0
    finally:
        thread.join(timeout=2)
        server.server_close()
