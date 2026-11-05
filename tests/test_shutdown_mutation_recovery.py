"""Shutdown/restart evidence for a mutating Spotify operation (#421).

All requests are in-memory fakes. These tests do not guarantee a real daemon thread
can complete after the interpreter or Docker terminates it.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _reconciliation import _desired, _FakeSpotify, _playlist

from news_bulletin_playlist import engine_runtime
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import EngineCycleResult
from news_bulletin_playlist.engine_runtime import EngineLifecycleController
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.reconciliation import (
    SpotifyReconciliationError,
    reconcile_spotify_playlist,
)
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics

NOW = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
PLAYLIST_ID = "test-shutdown"
OLD = "spotify:episode:old"
NEW = "spotify:episode:new"


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


class _BlockedMutationSpotify(_FakeSpotify):
    def __init__(self) -> None:
        super().__init__({PLAYLIST_ID: [OLD]})
        self.write_entered = threading.Event()
        self.release_write = threading.Event()

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        self.write_entered.set()
        if not self.release_write.wait(timeout=5):
            raise AssertionError("test failed to release the simulated remote mutation")
        return super().replace_playlist_items(playlist_id, uris)


class _ReconcileCycle:
    def __init__(self, client: _BlockedMutationSpotify, store: SQLiteStore) -> None:
        self.client = client
        self.store = store
        self.completed = threading.Event()
        self.calls = 0

    def run_cycle(self) -> EngineCycleResult:
        self.calls += 1
        result = reconcile_spotify_playlist(
            self.client,
            _playlist(PLAYLIST_ID),
            _desired(_playlist(PLAYLIST_ID), NEW),
            store=self.store,
        )
        assert result.wrote
        self.completed.set()
        return EngineCycleResult(
            started_at=NOW,
            finished_at=NOW,
            ok=True,
            sources=(),
            playlists=(),
        )


def test_shutdown_timeout_does_not_complete_blocked_remote_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_runtime, "_SCHEDULER_SHUTDOWN_WAIT_SECONDS", 0.02)
    store = _store(tmp_path)
    diagnostic_store = DiagnosticEventStore(store.path)
    diagnostic_store.initialize()
    diagnostics = OperationalDiagnostics(
        diagnostic_store, logger=logging.getLogger("test-421-remote-mutation")
    )
    spotify = _BlockedMutationSpotify()
    runner = _ReconcileCycle(spotify, store)
    controller = EngineLifecycleController(  # type: ignore[arg-type]
        runner, interval=timedelta(hours=1), diagnostics=diagnostics
    )
    controller.reconcile(configured=True)
    worker = controller._scheduler_thread
    assert worker is not None

    try:
        assert spotify.write_entered.wait(timeout=2)
        controller.shutdown()

        # The deadline is only a wait; shutdown does not cancel the in-flight PUT.
        assert worker.is_alive()
        assert controller.scheduler is None
        assert controller.status.snapshot().configured is False
        assert not runner.completed.is_set()
        assert spotify.writes == []
        assert any(
            event.event_name == "scheduler_shutdown_timeout"
            for event in diagnostic_store.list_events(limit=10)
        )
    finally:
        spotify.release_write.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert runner.completed.is_set()
    assert runner.calls == 1
    assert spotify.writes == [(PLAYLIST_ID, [NEW])]
    assert spotify.states[PLAYLIST_ID] == [NEW]


class _ExitAfterRemoteCommit(_FakeSpotify):
    def __init__(self) -> None:
        super().__init__({PLAYLIST_ID: [OLD]})
        self.exit_after_write = True

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        response = super().replace_playlist_items(playlist_id, uris)
        if self.exit_after_write:
            self.exit_after_write = False
            # Simulate process loss after the provider accepted the write,
            # but before the client read or persisted its confirmation.
            raise SystemExit("simulated process termination")
        return response


def test_restart_reads_remote_after_exit_before_write_confirmation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _ExitAfterRemoteCommit()
    playlist = _playlist(PLAYLIST_ID)
    desired = _desired(playlist, NEW)

    with pytest.raises(SystemExit, match="simulated process termination"):
        reconcile_spotify_playlist(client, playlist, desired, store=store)
    assert client.writes == [(PLAYLIST_ID, [NEW])]

    # A restart without read authority must fail closed, never blindly repeat PUT.
    client.fail_reads.add(PLAYLIST_ID)
    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    with pytest.raises(SpotifyReconciliationError, match="prewrite"):
        reconcile_spotify_playlist(client, playlist, desired, store=restarted_store)
    assert len(client.writes) == 1

    # When the destination is readable and matches, the new process skips the write.
    client.fail_reads.clear()
    result = reconcile_spotify_playlist(client, playlist, desired, store=restarted_store)
    assert result.wrote is False
    assert client.states[PLAYLIST_ID] == [NEW]
    assert len(client.writes) == 1
