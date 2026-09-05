from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.engine import (
    EngineCycleAlreadyRunning,
    EngineCycleResult,
    EngineRunner,
    EngineScheduler,
    OperationalStatus,
)
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
from news_bulletin_playlist.spotify.auth import SpotifyReauthorizationRequired
from news_bulletin_playlist.spotify.client import SpotifyTransportError

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
SER_URL = "https://example.test/ser.xml"
SER_TITLE = "Las noticias de la SER, 11:00 (30/08/2026)"
SER_URI = "spotify:episode:ser-1100"


class _Auth:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        assert now is not None
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "access-token"


class _Spotify:
    def __init__(self, *, fail_playlist: str | None = None) -> None:
        self.fail_playlist = fail_playlist
        self.show_calls = 0
        self.playlist_reads: list[str] = []
        self.replacements: list[tuple[str, tuple[str, ...]]] = []
        self.items: dict[str, list[str]] = {}
        self.snapshots: dict[str, str] = {}

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "ser-show"
        assert limit == 50
        assert offset == 0
        self.show_calls += 1
        return {
            "items": [
                {
                    "uri": SER_URI,
                    "name": SER_TITLE,
                    "release_date": "2026-08-30",
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
        assert limit in {1, 50}
        self.playlist_reads.append(playlist_id)
        if playlist_id == self.fail_playlist:
            raise SpotifyTransportError("simulated playlist outage")
        uris = self.items.get(playlist_id, [])
        selected = uris[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in selected],
            "next": None,
            "total": len(uris),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        if playlist_id == self.fail_playlist:
            raise SpotifyTransportError("simulated playlist outage")
        self.items[playlist_id] = list(uris)
        self.replacements.append((playlist_id, tuple(uris)))
        snapshot = f"snapshot-{len(self.replacements)}"
        self.snapshots[playlist_id] = snapshot
        return {"snapshot_id": snapshot}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        return {"snapshot_id": self.snapshots.get(playlist_id, "snapshot-initial")}


def _source() -> SourceDefinition:
    return SourceDefinition(
        id=SourceId("ser"),
        display_name="SER",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ser"),
        endpoint_url=SER_URL,
        external_references=(
            ExternalReference(
                system="spotify",
                resource_type="show",
                external_id="ser-show",
            ),
        ),
    )


def _playlist(playlist_id: str) -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId(playlist_id),
        display_name=playlist_id,
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SourceId("ser"),)),
        destination=DestinationReference(
            adapter_id=AdapterId("spotify"),
            external_id=f"destination-{playlist_id}",
        ),
    )


def _config(*playlist_ids: str) -> EngineConfig:
    return EngineConfig(
        schema_version=1,
        sources=(_source(),),
        playlists=tuple(_playlist(value) for value in playlist_ids),
    )


def _rss() -> bytes:
    return (
        "<rss><channel><item>"
        "<guid>ser-1</guid>"
        f"<title>{SER_TITLE}</title>"
        "<pubDate>Sun, 30 Aug 2026 09:05:00 +0000</pubDate>"
        "</item></channel></rss>"
    ).encode()


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    return store


def test_cycle_fetches_shared_source_once_and_reconciles_two_playlists(tmp_path: Path) -> None:
    calls: list[str] = []
    spotify = _Spotify()

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return _rss()

    runner = EngineRunner(
        _config("first", "second"),
        _store(tmp_path),
        _Auth(),
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    first = runner.run_cycle()
    second = runner.run_cycle()

    assert first.ok
    assert second.ok
    assert calls == [SER_URL, SER_URL]
    assert spotify.show_calls == 1
    assert spotify.replacements == [
        ("destination-first", (SER_URI,)),
        ("destination-second", (SER_URI,)),
    ]
    assert all(outcome.desired_count == 1 for outcome in second.playlists)
    assert all(outcome.wrote is False for outcome in second.playlists)


def test_source_failure_carries_forward_recent_durable_match(tmp_path: Path) -> None:
    fail = False
    spotify = _Spotify()

    def fetcher(_url: str) -> bytes:
        if fail:
            raise OSError("source unavailable")
        return _rss()

    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    first = runner.run_cycle()
    fail = True
    second = runner.run_cycle()

    assert first.ok
    assert not second.ok
    assert not second.sources[0].collection_ok
    assert "source unavailable" in (second.sources[0].error or "")
    assert second.playlists[0].ok
    assert second.playlists[0].desired_count == 1
    assert second.playlists[0].wrote is False
    assert spotify.items["destination-first"] == [SER_URI]


def test_first_source_failure_preserves_existing_destination_without_lkg(
    tmp_path: Path,
) -> None:
    spotify = _Spotify()
    existing_uri = "spotify:episode:preexisting"
    spotify.items["destination-first"] = [existing_uri]
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: (_ for _ in ()).throw(OSError("source unavailable")),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    assert not result.playlists[0].ok
    assert result.playlists[0].wrote is None
    assert "destination preserved" in (result.playlists[0].error or "")
    assert spotify.items["destination-first"] == [existing_uri]
    assert spotify.playlist_reads == []
    assert spotify.replacements == []


def test_first_matching_failure_preserves_existing_destination_without_lkg(
    tmp_path: Path,
) -> None:
    existing_uri = "spotify:episode:preexisting"

    class MatchingUnavailableSpotify(_Spotify):
        def show_episodes(
            self,
            show_id: str,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> dict[str, Any]:
            del show_id, limit, offset
            raise SpotifyTransportError("simulated catalogue outage")

    spotify = MatchingUnavailableSpotify()
    spotify.items["destination-first"] = [existing_uri]
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    assert result.sources[0].matching_ok is False
    assert not result.playlists[0].ok
    assert result.playlists[0].wrote is None
    assert "destination preserved" in (result.playlists[0].error or "")
    assert spotify.items["destination-first"] == [existing_uri]
    assert spotify.playlist_reads == []
    assert spotify.replacements == []


def test_playlist_failure_does_not_block_other_destination(tmp_path: Path) -> None:
    spotify = _Spotify(fail_playlist="destination-first")
    runner = EngineRunner(
        _config("first", "second"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    by_id = {outcome.playlist_id: outcome for outcome in result.playlists}
    assert not by_id[PlaylistId("first")].ok
    assert by_id[PlaylistId("second")].ok
    assert spotify.items["destination-second"] == [SER_URI]


def test_auth_failure_is_reported_without_empty_success(tmp_path: Path) -> None:
    auth = _Auth(SpotifyReauthorizationRequired("Spotify authorization is required"))
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: pytest.fail("client must not be created"),
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    assert result.error == "Spotify authorization is required"
    assert result.sources[0].collection_ok
    assert result.playlists[0].error == "Spotify authorization is required"
    assert auth.calls == 1


def test_access_token_is_consumed_but_never_exposed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    access_token = "access-token-sentinel"
    spotify = _Spotify(fail_playlist="destination-first")

    class SentinelAuth:
        def get_access_token(self, *, now: datetime | None = None) -> str:
            assert now is not None
            return access_token

    def client_factory(token: str) -> _Spotify:
        assert token == access_token
        return spotify

    result = EngineRunner(
        _config("first"),
        _store(tmp_path),
        SentinelAuth(),
        fetcher=lambda _url: _rss(),
        client_factory=client_factory,
        clock=lambda: NOW,
    ).run_cycle()

    captured = capsys.readouterr()
    assert not result.ok
    assert access_token not in repr(result)
    assert access_token not in captured.out
    assert access_token not in captured.err


def test_concurrent_cycle_is_rejected(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def fetcher(_url: str) -> bytes:
        entered.set()
        assert release.wait(timeout=2)
        return _rss()

    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=fetcher,
        client_factory=lambda _token: _Spotify(),
        clock=lambda: NOW,
    )
    thread = threading.Thread(target=runner.run_cycle)
    thread.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(EngineCycleAlreadyRunning):
            runner.run_cycle()
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_scheduler_wake_runs_early_and_stop_prevents_another_cycle() -> None:
    calls = 0
    first_done = threading.Event()
    second_done = threading.Event()

    class Runner:
        def run_cycle(self) -> EngineCycleResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_done.set()
            if calls == 2:
                second_done.set()
            return EngineCycleResult(NOW, NOW, True, (), ())

    status = OperationalStatus(configured=True)
    scheduler = EngineScheduler(
        Runner(),
        status,
        interval=timedelta(hours=1),
        clock=lambda: NOW,
    )
    stop = threading.Event()
    thread = threading.Thread(target=scheduler.run, args=(stop,))
    thread.start()
    assert first_done.wait(timeout=2)
    scheduler.wake()
    assert second_done.wait(timeout=2)
    stop.set()
    scheduler.wake()
    thread.join(timeout=2)

    assert calls == 2
    assert not thread.is_alive()
    assert status.snapshot().next_run_at is None


def test_scheduler_stop_event_interrupts_wait_without_explicit_wake() -> None:
    calls = 0
    first_done = threading.Event()

    class Runner:
        def run_cycle(self) -> EngineCycleResult:
            nonlocal calls
            calls += 1
            first_done.set()
            return EngineCycleResult(NOW, NOW, True, (), ())

    status = OperationalStatus(configured=True)
    scheduler = EngineScheduler(
        Runner(),
        status,
        interval=timedelta(hours=1),
        clock=lambda: NOW,
    )
    stop = threading.Event()
    thread = threading.Thread(target=scheduler.run, args=(stop,))
    thread.start()
    assert first_done.wait(timeout=2)

    stop.set()
    thread.join(timeout=2)

    assert calls == 1
    assert not thread.is_alive()
    assert status.snapshot().next_run_at is None


def test_missing_spotify_duration_preserves_existing_destination_without_write(
    tmp_path: Path,
) -> None:
    existing_uri = "spotify:episode:preexisting"

    class MissingDurationSpotify(_Spotify):
        def show_episodes(
            self,
            show_id: str,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> dict[str, Any]:
            page = super().show_episodes(show_id, limit=limit, offset=offset)
            item = page["items"][0]
            assert isinstance(item, dict)
            item.pop("duration_ms")
            return page

    spotify = MissingDurationSpotify()
    spotify.items["destination-first"] = [existing_uri]
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    assert result.sources[0].matching_ok is True
    assert not result.playlists[0].ok
    assert result.playlists[0].wrote is None
    assert "Spotify duration unavailable" in (result.playlists[0].error or "")
    assert spotify.items["destination-first"] == [existing_uri]
    assert spotify.playlist_reads == []
    assert spotify.replacements == []
