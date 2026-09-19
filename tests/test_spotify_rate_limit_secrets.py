from __future__ import annotations

import io
import urllib.error

import pytest

from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyClient

ACCESS_TOKEN_SENTINEL = "access-token-sentinel-never-escape"
PROVIDER_BODY_SENTINEL = "provider-body-sentinel-never-escape"


def test_http_429_does_not_emit_token_or_provider_body(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_urlopen(request: object, *, timeout: float):  # type: ignore[no-untyped-def]
        del timeout
        assert hasattr(request, "full_url")
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "rate limited",
            {"Retry-After": "12"},
            io.BytesIO(PROVIDER_BODY_SENTINEL.encode()),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(SpotifyApiError) as error:
        SpotifyClient(ACCESS_TOKEN_SENTINEL).search_shows("query")

    captured = capsys.readouterr()
    exposed = "\n".join((str(error.value), captured.out, captured.err))
    assert PROVIDER_BODY_SENTINEL not in exposed
    assert ACCESS_TOKEN_SENTINEL not in exposed
