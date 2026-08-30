from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.spotify import auth as auth_module
from news_bulletin_playlist.spotify.auth import (
    PRODUCTION_SCOPES,
    SpotifyAuthService,
    SpotifyCredentialStore,
    SpotifyOAuthCallbackError,
    TokenResponse,
)


class _NeverExchangeTransport:
    def exchange_code(
        self,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> TokenResponse:
        del client_id, code, redirect_uri, verifier
        raise AssertionError("expired callback must not reach token exchange")

    def refresh_token(self, *, client_id: str, refresh_token: str) -> TokenResponse:
        del client_id, refresh_token
        raise AssertionError("refresh is not expected")


def test_expired_callback_does_not_expose_pkce_or_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = "pkce-verifier-sentinel-abcdefghijklmnopqrstuvwxyz0123456789ABCDE"
    code = "expired-authorization-code-sentinel"
    monkeypatch.setattr(auth_module, "create_code_verifier", lambda: verifier)

    redirect_uri = "http://127.0.0.1:8787/admin/spotify/callback"
    service = SpotifyAuthService(
        client_id="client-id",
        redirect_uri=redirect_uri,
        store=SpotifyCredentialStore(tmp_path / "spotify-auth.json"),
        transport=_NeverExchangeTransport(),
        scopes=PRODUCTION_SCOPES,
    )
    started_at = datetime(2000, 1, 1, tzinfo=UTC)
    authorize_url = service.start_authorization(now=started_at)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)["state"][0]
    query = urllib.parse.urlencode({"state": state, "code": code})
    callback_url = f"{redirect_uri}?{query}"

    with pytest.raises(SpotifyOAuthCallbackError) as raised:
        service.complete_callback(query, now=started_at + timedelta(minutes=11))

    captured = capsys.readouterr()
    user_visible = str(raised.value)
    assert "expired" in user_visible.lower()
    for secret in (verifier, code, callback_url, query):
        assert secret not in user_visible
        assert secret not in captured.out
        assert secret not in captured.err
