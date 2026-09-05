from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.desired_state import DesiredPlaylistItem, DesiredPlaylistState
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
from news_bulletin_playlist.reconciliation import (
    SpotifyReconciliationError,
    reconcile_spotify_playlist,
)
from news_bulletin_playlist.spotify.client import SpotifyApiError
from news_bulletin_playlist.spotify_backoff import (
    DEFAULT_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS,
    SpotifyRateLimitBackoffActive,
    SpotifyRateLimitGuardAuth,
    SpotifyRateLimitGuardClient,
    SpotifyRateLimitJournal,
    SpotifyRateLimitSuppressed,
)

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)


class _Auth:
    def __init__(self) -> None:
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        assert now is not None
        self.calls += 1
        return "access-token"


class _Spotify:
    def __init__(
        self,
        *,
        fail_show: str | None = None,
        fail_playlist: str | None = None,
        fail_snapshot: bool = False,
        retry_after: int | None = 120,
    ) -> None:
        self.fail_show = fail_show
        self.fail_playlist = fail_playlist
        self.fail_snapshot = fail_snapshot
        self.retry_after = retry_after
        self.show_calls: list[str] = []
        self.playlist_calls: list[str] = []
        self.snapshot_calls: list[str] = []
        self.replace_calls: list[str] = []
        self.items: dict[str, list[str]] = {}

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del limit, offset
        self.show_calls.append(show_id)
        if show_id == self.fail_show:
            raise SpotifyApiError(429, "rate limited", retry_after=self.retry_after)
        source_id = show_id.removesuffix("-show")
        return {
            "items": [
                {
                    "uri": f"spotify:episode:{source_id}",
                    "name": f"Bulletin {source_id}",
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
        self.playlist_calls.append(playlist_id)
        if playlist_id == self.fail_playlist:
            raise SpotifyApiError(429, "rate limited", retry_after=self.retry_after)
        items = self.items.get(playlist_id, [])
        selected = items[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in selected],
            "next": None,
            "total": len(items),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        self.replace_calls.append(playlist_id)
        self.items[playlist_id] = list(uris)
        return {"snapshot_id": "snapshot-write"}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        self.snapshot_calls.append(playlist_id)
        if self.fail_snapshot:
            raise SpotifyApiError(429, "rate limited", retry_after=self.retry_after)
        return {"snapshot_id": "snapshot-current"}


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    return store


def _source(source_id: str) -> SourceDefinition:
    return SourceDefinition(
        id=SourceId(source_id),
        display_name=source_id,
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es-ES"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url=f"https://example.test/{source_id}.xml",
        external_references=(
            ExternalReference("spotify", "show", f"{source_id}-show"),
        ),
    )


def _playlist(playlist_id: str, *source_ids: str) -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId(playlist_id),
        display_name=playlist_id,
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es-ES"),),
        enabled=True,
        source_selection=SourceSelection(tuple(SourceId(value) for value in source_ids)),
        destination=DestinationReference(
            adapter_id=AdapterId("spotify"),
            external_id=f"destination-{playlist_id}",
        ),
    )


def _config(
    sources: tuple[SourceDefinition, ...],
    playlists: tuple[PlaylistDefinition, ...],
) -> EngineConfig:
    return EngineConfig(schema_version=1, sources=sources, playlists=playlists)


def _rss(source_id: str) -> bytes:
    return (
        "<rss><channel><item>"
        f"<guid>{source_id}-1</guid>"
        f"<title>Bulletin {source_id}</title>"
        "<pubDate>Sat, 05 Sep 2026 09:30:00 +0000</pubDate>"
        "</item></channel></rss>"
    ).encode()


def test_journal_honors_header_fallback_and_never_shortens_deadline(tmp_path: Path) -> None:
    journal = SpotifyRateLimitJournal(_store(tmp_path))

    first = journal.activate(observed_at=NOW, retry_after_seconds=3600)
    assert first.retry_not_before == NOW + timedelta(hours=1)
    assert first.retry_after_seconds == 3600
    assert first.backoff_source == "spotify_header"

    shorter = journal.activate(observed_at=NOW + timedelta(seconds=10), retry_after_seconds=30)
    assert shorter == first

    fallback = journal.activate(observed_at=NOW + timedelta(hours=2), retry_after_seconds=None)
    assert fallback.retry_not_before == NOW + timedelta(
        hours=2, seconds=DEFAULT_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS
    )
    assert fallback.retry_after_seconds is None
    assert fallback.backoff_source == "fallback"


def test_same_cycle_retry_after_zero_still_suppresses_every_later_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    delegate = _Spotify(fail_show="one-show", retry_after=0)
    client = SpotifyRateLimitGuardClient(
        delegate,
        SpotifyRateLimitJournal(store),
        clock=lambda: NOW,
    )

    with pytest.raises(SpotifyApiError, match="429"):
        client.show_episodes("one-show")
    with pytest.raises(SpotifyRateLimitSuppressed):
        client.playlist_items("destination-one")

    assert delegate.show_calls == ["one-show"]
    assert delegate.playlist_calls == []
    assert delegate.replace_calls == []


def test_durable_active_backoff_blocks_auth_then_expires(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = SpotifyRateLimitJournal(store)
    journal.activate(observed_at=NOW, retry_after_seconds=600)
    auth = _Auth()

    restarted = SpotifyRateLimitGuardAuth(
        auth,
        SpotifyRateLimitJournal(store),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    with pytest.raises(SpotifyRateLimitBackoffActive, match="backoff active until"):
        restarted.get_access_token(now=NOW + timedelta(minutes=1))
    assert auth.calls == 0

    resumed = SpotifyRateLimitGuardAuth(
        auth,
        SpotifyRateLimitJournal(store),
        clock=lambda: NOW + timedelta(minutes=11),
    )
    assert resumed.get_access_token(now=NOW + timedelta(minutes=11)) == "access-token"
    assert auth.calls == 1
    assert SpotifyRateLimitJournal(store).get() is None


def test_engine_active_backoff_keeps_rss_collection_but_performs_zero_spotify_io(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    SpotifyRateLimitJournal(store).activate(observed_at=NOW, retry_after_seconds=600)
    auth = _Auth()
    spotify_factory_calls = 0
    fetch_calls: list[str] = []

    def fetcher(url: str) -> bytes:
        fetch_calls.append(url)
        return _rss("one")

    def client_factory(_token: str) -> _Spotify:
        nonlocal spotify_factory_calls
        spotify_factory_calls += 1
        return _Spotify()

    result = EngineRunner(
        _config((_source("one"),), (_playlist("one", "one"),)),
        store,
        auth,
        fetcher=fetcher,
        client_factory=client_factory,
        clock=lambda: NOW + timedelta(minutes=1),
    ).run_cycle()

    assert fetch_calls == ["https://example.test/one.xml"]
    assert auth.calls == 0
    assert spotify_factory_calls == 0
    assert not result.ok
    assert result.sources[0].collection_ok
    assert result.sources[0].matching_ok is None
    assert result.playlists[0].wrote is None
    assert "rate-limit backoff active" in (result.playlists[0].error or "")


def test_matching_429_stops_later_live_catalogue_requests(tmp_path: Path) -> None:
    store = _store(tmp_path)
    delegate = _Spotify(fail_show="one-show", retry_after=300)
    auth = _Auth()

    result = EngineRunner(
        _config(
            (_source("one"), _source("two")),
            (_playlist("mixed", "one", "two"),),
        ),
        store,
        auth,
        fetcher=lambda url: _rss("one" if url.endswith("one.xml") else "two"),
        client_factory=lambda _token: delegate,
        clock=lambda: NOW,
    ).run_cycle()

    assert not result.ok
    assert delegate.show_calls == ["one-show"]
    assert delegate.playlist_calls == []
    assert delegate.replace_calls == []
    active = SpotifyRateLimitJournal(store).active(now=NOW + timedelta(seconds=1))
    assert active is not None
    assert active.retry_not_before == NOW + timedelta(seconds=300)


def test_playlist_429_stops_later_live_destination_requests(tmp_path: Path) -> None:
    store = _store(tmp_path)
    delegate = _Spotify(fail_playlist="destination-first", retry_after=300)

    result = EngineRunner(
        _config(
            (_source("one"),),
            (_playlist("first", "one"), _playlist("second", "one")),
        ),
        store,
        _Auth(),
        fetcher=lambda _url: _rss("one"),
        client_factory=lambda _token: delegate,
        clock=lambda: NOW,
    ).run_cycle()

    assert not result.ok
    assert delegate.show_calls == ["one-show"]
    assert delegate.playlist_calls == ["destination-first"]
    assert delegate.replace_calls == []
    assert all(outcome.wrote is None for outcome in result.playlists)


def test_optional_snapshot_429_cannot_reach_live_replace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = SpotifyRateLimitJournal(store)
    delegate = _Spotify(fail_snapshot=True, retry_after=300)
    client = SpotifyRateLimitGuardClient(delegate, journal, clock=lambda: NOW)
    playlist = _playlist("one", "one")
    desired = DesiredPlaylistState(
        playlist_id=playlist.id,
        generated_at=NOW,
        items=(
            DesiredPlaylistItem(
                source_id=SourceId("one"),
                source_native_id="one-1",
                published_at=NOW,
                spotify_episode_uri="spotify:episode:one",
            ),
        ),
    )

    with pytest.raises(SpotifyReconciliationError, match="transport failure"):
        reconcile_spotify_playlist(client, playlist, desired, store=store)

    assert delegate.playlist_calls == ["destination-one"]
    assert delegate.snapshot_calls == ["destination-one"]
    assert delegate.replace_calls == []
    assert journal.active(now=NOW + timedelta(seconds=1)) is not None


def test_backoff_diagnostics_are_sanitized_and_distinguish_activation_from_active(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    journal = SpotifyRateLimitJournal(store)
    delegate = _Spotify(fail_show="one-show", retry_after=90)
    client = SpotifyRateLimitGuardClient(delegate, journal, clock=lambda: NOW)

    with pytest.raises(SpotifyApiError):
        client.show_episodes("one-show")

    auth = _Auth()
    guarded_auth = SpotifyRateLimitGuardAuth(
        auth,
        SpotifyRateLimitJournal(store),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    with pytest.raises(SpotifyRateLimitBackoffActive):
        guarded_auth.get_access_token(now=NOW + timedelta(seconds=1))

    events = tuple(
        event
        for event in DiagnosticEventStore(store.path).list_events(limit=20)
        if event.event_name == "spotify_rate_limit_backoff"
    )
    assert len(events) == 2
    by_state = {str(event.details["backoff_state"]): event for event in events}
    assert set(by_state) == {"activated", "active"}
    for event in events:
        assert event.component == "spotify"
        assert event.details["http_status"] == 429
        assert event.details["retry_after_seconds"] == 90
        assert event.details["retry_not_before"] == "2026-09-05T10:01:30Z"
        assert event.details["backoff_source"] == "spotify_header"
        assert event.details["write_decision"] == "skipped"
        assert "token" not in repr(event).casefold()
