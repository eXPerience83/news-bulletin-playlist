from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
from news_bulletin_playlist.spotify.client import SpotifyApiError
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
RSS_URL = "https://example.test/news.xml"
TITLE = "Daily bulletin"
URI = "spotify:episode:daily"


class _Auth:
    def __init__(self) -> None:
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        assert now is not None
        self.calls += 1
        return "access-token"


class _RateLimitedCatalogue:
    def __init__(self) -> None:
        self.calls = 0

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del show_id, limit, offset
        self.calls += 1
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


class _WorkingSpotify:
    def __init__(self) -> None:
        self.show_calls = 0
        self.playlist_reads = 0
        self.replacements = 0
        self.items: list[str] = []

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "daily-show"
        assert limit == 50
        assert offset == 0
        self.show_calls += 1
        return {
            "items": [
                {
                    "uri": URI,
                    "name": TITLE,
                    "release_date": "2026-09-05",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
            ],
            "next": None,
        }

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert playlist_id == "destination-daily"
        self.playlist_reads += 1
        selected = self.items[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in selected],
            "next": None,
            "total": len(self.items),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        assert playlist_id == "destination-daily"
        self.replacements += 1
        self.items = list(uris)
        return {"snapshot_id": "snapshot-write"}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        assert playlist_id == "destination-daily"
        return {"snapshot_id": "snapshot-write"}


def _config() -> EngineConfig:
    source = SourceDefinition(
        id=SourceId("daily"),
        display_name="Daily",
        countries=(CountryCode("GB"),),
        languages=(LanguageTag("en"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url=RSS_URL,
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
        f"<title>{TITLE}</title>"
        "<pubDate>Sat, 05 Sep 2026 09:30:00 +0000</pubDate>"
        "</item></channel></rss>"
    ).encode()


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    return store


def test_engine_restart_keeps_cooldown_then_resumes_after_deadline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_auth = _Auth()
    limited = _RateLimitedCatalogue()
    fetch_count = 0

    def fetcher(_url: str) -> bytes:
        nonlocal fetch_count
        fetch_count += 1
        return _rss()

    first = EngineRunner(
        _config(),
        store,
        first_auth,
        fetcher=fetcher,
        client_factory=lambda _token: limited,
        clock=lambda: NOW,
    ).run_cycle()

    assert not first.ok
    assert first_auth.calls == 1
    assert limited.calls == 1
    state = SpotifyRateLimitJournal(store).get()
    assert state is not None
    assert state.retry_not_before == NOW + timedelta(minutes=5)

    restarted_auth = _Auth()
    factory_calls = 0

    def forbidden_factory(_token: str) -> _WorkingSpotify:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("Spotify client must not be created during active backoff")

    second = EngineRunner(
        _config(),
        SQLiteStore(store.path),
        restarted_auth,
        fetcher=fetcher,
        client_factory=forbidden_factory,
        clock=lambda: NOW + timedelta(minutes=1),
    ).run_cycle()

    assert not second.ok
    assert fetch_count == 2
    assert restarted_auth.calls == 0
    assert factory_calls == 0

    resumed_auth = _Auth()
    working = _WorkingSpotify()
    third = EngineRunner(
        _config(),
        SQLiteStore(store.path),
        resumed_auth,
        fetcher=fetcher,
        client_factory=lambda _token: working,
        clock=lambda: NOW + timedelta(minutes=6),
    ).run_cycle()

    assert third.ok
    assert fetch_count == 3
    assert resumed_auth.calls == 1
    assert working.show_calls == 1
    assert working.replacements == 1
    assert working.items == [URI]
    assert SpotifyRateLimitJournal(store).get() is None
