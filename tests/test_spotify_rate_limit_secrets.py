from __future__ import annotations

import io
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import (
    AdapterId,
    CountryCode,
    DestinationReference,
    EngineConfig,
    ExternalReference,
    LanguageTag,
    ParserId,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyClient
from news_bulletin_playlist.spotify_backoff import (
    SpotifyRateLimitBackoffActive,
    SpotifyRateLimitGuardAuth,
    SpotifyRateLimitJournal,
)

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
OAUTH_SENTINELS = (
    "access-token-sentinel-never-escape",
    "refresh-token-sentinel-never-escape",
    "authorization-code-sentinel-never-escape",
    "pkce-verifier-sentinel-never-escape",
)
PROVIDER_BODY_SENTINEL = "provider-body-sentinel-never-escape"


class _SecretAuth:
    def __init__(self) -> None:
        self.access_token = OAUTH_SENTINELS[0]
        self.refresh_token = OAUTH_SENTINELS[1]
        self.authorization_code = OAUTH_SENTINELS[2]
        self.pkce_verifier = OAUTH_SENTINELS[3]
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        assert now is not None
        self.calls += 1
        return self.access_token


class _RateLimitedSpotify:
    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del show_id, limit, offset
        raise SpotifyApiError(429, "rate limited", retry_after=300)

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        raise AssertionError((playlist_id, limit, offset))

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        raise AssertionError((playlist_id, uris))

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        raise AssertionError(playlist_id)


def _config() -> EngineConfig:
    source = SourceDefinition(
        id=SourceId("daily"),
        display_name="Daily",
        countries=(CountryCode("GB"),),
        languages=(LanguageTag("en"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url="https://example.test/news.xml",
        external_references=(ExternalReference("spotify", "show", "daily-show"),),
    )
    playlist = PlaylistDefinition(
        id=PlaylistId("daily"),
        display_name="Daily",
        description="test",
        countries=(CountryCode("GB"),),
        languages=(LanguageTag("en"),),
        enabled=True,
        source_selection=SourceSelection((source.id,)),
        destination=DestinationReference(
            adapter_id=AdapterId("spotify"),
            external_id="destination-daily",
        ),
    )
    return EngineConfig(schema_version=1, sources=(source,), playlists=(playlist,))


def _rss() -> bytes:
    return (
        "<rss><channel><item>"
        "<guid>daily-1</guid>"
        "<title>Daily bulletin</title>"
        "<pubDate>Sat, 05 Sep 2026 09:30:00 +0000</pubDate>"
        "</item></channel></rss>"
    ).encode()


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    return store


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
        SpotifyClient(OAUTH_SENTINELS[0]).search_shows("query")

    captured = capsys.readouterr()
    exposed = "\n".join((str(error.value), captured.out, captured.err))
    assert PROVIDER_BODY_SENTINEL not in exposed
    assert OAUTH_SENTINELS[0] not in exposed


def test_engine_and_backoff_exception_do_not_disclose_oauth_secrets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path)
    auth = _SecretAuth()
    limited = _RateLimitedSpotify()

    result = EngineRunner(
        _config(),
        store,
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda token: limited if token == OAUTH_SENTINELS[0] else limited,
        clock=lambda: NOW,
    ).run_cycle()

    assert not result.ok
    assert auth.calls == 1
    guard = SpotifyRateLimitGuardAuth(
        auth,
        SpotifyRateLimitJournal(store),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    with pytest.raises(SpotifyRateLimitBackoffActive) as error:
        guard.get_access_token(now=NOW + timedelta(seconds=1))

    captured = capsys.readouterr()
    events = DiagnosticEventStore(store.path).list_events(limit=50)
    exposed = "\n".join(
        (
            captured.out,
            captured.err,
            result.error or "",
            *(outcome.error or "" for outcome in result.sources),
            *(outcome.error or "" for outcome in result.playlists),
            str(error.value),
            repr(events),
        )
    )
    for sentinel in OAUTH_SENTINELS:
        assert sentinel not in exposed
