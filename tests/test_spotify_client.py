from __future__ import annotations

import io
import urllib.error
import urllib.parse

import pytest

from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_show_episode_limit_guard() -> None:
    client = SpotifyClient("token")
    with pytest.raises(ValueError):
        client.show_episodes("show", limit=51)


def test_search_limit_guard() -> None:
    client = SpotifyClient("token")
    with pytest.raises(ValueError):
        client.search_shows("query", limit=11)


def test_replace_playlist_hard_limit() -> None:
    client = SpotifyClient("token")
    with pytest.raises(ValueError):
        client.replace_playlist_items("playlist", [f"spotify:episode:{i}" for i in range(101)])


def test_playlist_read_limit_guard() -> None:
    client = SpotifyClient("token")
    with pytest.raises(ValueError):
        client.playlist_items("playlist", limit=101)


def test_client_sends_authorization_market_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        requests.append(request)
        return _Response(b'{"items": []}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    SpotifyClient("test-token", market="GB").show_episodes("show", limit=10, offset=20)
    request = requests[0]
    assert request.headers["Authorization"] == "Bearer test-token"
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
    assert query == {"market": ["GB"], "limit": ["10"], "offset": ["20"]}


def test_client_omits_market_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        assert "market=" not in request.full_url
        return _Response(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    SpotifyClient("token").search_shows("query")


def test_client_write_and_readback_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        requests.append(request)
        return _Response(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = SpotifyClient("token")
    client.replace_playlist_items("playlist", ["spotify:episode:one"])
    client.playlist_items("playlist", limit=50, offset=10)
    assert requests[0].method == "PUT"
    assert requests[1].method == "GET"


@pytest.mark.parametrize("status", [400, 401, 403, 429])
def test_client_sanitizes_http_errors(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        raise urllib.error.HTTPError(
            request.full_url, status, "bad", {"Retry-After": "12"}, io.BytesIO(b"token")
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(SpotifyApiError) as error:
        SpotifyClient("test-token").search_shows("query")
    assert error.value.status == status
    assert "test-token" not in str(error.value)
    assert error.value.retry_after == 12


def test_client_handles_network_and_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def network_error(request: object, *, timeout: float) -> _Response:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", network_error)
    with pytest.raises(SpotifyTransportError):
        SpotifyClient("token").search_shows("query")

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: _Response(b"not json"))
    with pytest.raises(SpotifyApiError, match="invalid JSON"):
        SpotifyClient("token").search_shows("query")
