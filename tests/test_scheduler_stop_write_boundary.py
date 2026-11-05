"""A scheduler stop before the mutation phase must prevent a new Spotify write."""

from __future__ import annotations

import threading
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _reconciliation import _desired, _FakeSpotify, _playlist

from news_bulletin_playlist import engine_runtime
from news_bulletin_playlist.engine import (
    EngineCycleResult,
    EngineScheduler,
    OperationalStatus,
)
from news_bulletin_playlist.engine_runtime import (
    ConfigurationSynchronization,
    ReloadingEngineCycleRunner,
)
from news_bulletin_playlist.models import EngineConfig
from news_bulletin_playlist.reconciliation import reconcile_spotify_playlist

NOW = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
PLAYLIST_ID = "stop-boundary"


class _PrewriteHeldSpotify(_FakeSpotify):
    def __init__(self) -> None:
        super().__init__({PLAYLIST_ID: ["spotify:episode:old"]})
        self.read_entered = threading.Event()
        self.release_read = threading.Event()

    def playlist_items(
        self, playlist_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        self.read_entered.set()
        if not self.release_read.wait(timeout=4):
            raise AssertionError("test did not release prewrite Spotify read")
        return super().playlist_items(playlist_id, limit=limit, offset=offset)


class _GuardedRunner:
    def __init__(
        self,
        spotify: _PrewriteHeldSpotify,
        tmp_path: Path,
        synchronization: ConfigurationSynchronization | None,
    ) -> None:
        self.spotify = spotify
        self.playlist = _playlist(PLAYLIST_ID)
        self.desired = _desired(self.playlist, "spotify:episode:new")
        self.config = EngineConfig(sources=(), playlists=(self.playlist,))
        self.guard = object.__new__(ReloadingEngineCycleRunner)
        self.guard.data_dir = tmp_path
        self.guard.environ = {}
        self.guard.configuration_synchronization = synchronization

    def run_cycle(self) -> EngineCycleResult:
        reconcile_spotify_playlist(
            self.spotify,
            self.playlist,
            self.desired,
            mutation_guard=lambda: self.guard._playlist_mutation_guard(self.config, self.playlist),
        )
        return EngineCycleResult(started_at=NOW, finished_at=NOW, ok=True, sources=(), playlists=())


@pytest.mark.parametrize("configuration_lock_held", [False, True])
def test_stop_during_prewrite_blocks_new_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configuration_lock_held: bool,
) -> None:
    synchronization = ConfigurationSynchronization() if configuration_lock_held else None
    spotify = _PrewriteHeldSpotify()
    runner = _GuardedRunner(spotify, tmp_path, synchronization)

    def forbid_config_load(*args: object) -> None:
        raise AssertionError("shutdown must prevent a new write before config reload")

    monkeypatch.setattr(engine_runtime, "_load_runtime_config", forbid_config_load)
    stop = threading.Event()
    status = OperationalStatus(configured=True)
    scheduler = EngineScheduler(runner, status, interval=timedelta(hours=1))
    worker = threading.Thread(target=scheduler.run, args=(stop,), daemon=True)

    try:
        held = synchronization.hold() if synchronization is not None else nullcontext()
        with held:
            worker.start()
            assert spotify.read_entered.wait(timeout=2)
            stop.set()
            scheduler.wake()
            spotify.release_read.set()
    finally:
        stop.set()
        spotify.release_read.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert spotify.writes == []
    assert spotify.states[PLAYLIST_ID] == ["spotify:episode:old"]
    last = status.snapshot().last_cycle
    assert last is not None
    assert last.ok is False
    assert last.error == "unexpected engine failure: SpotifyReconciliationError"
