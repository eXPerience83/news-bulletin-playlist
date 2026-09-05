from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import EngineConfig
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.spotify.client import SpotifyApiError
from news_bulletin_playlist.spotify_backoff import (
    SpotifyRateLimitGuardClient,
    SpotifyRateLimitJournal,
)

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)


class _Auth:
    def __init__(self) -> None:
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        del now
        self.calls += 1
        return "access-token"


class _Slow429:
    def __init__(self, current_time: list[datetime]) -> None:
        self.current_time = current_time
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
        self.current_time[0] += timedelta(seconds=20)
        raise SpotifyApiError(429, "rate limited", retry_after=30)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    return store


def test_sqlite_error_inside_backoff_context_fails_closed_without_escaping_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    auth = _Auth()
    client_factory_calls = 0

    def client_factory(_access_token: str) -> Any:
        nonlocal client_factory_calls
        client_factory_calls += 1
        raise AssertionError("Spotify client must not be created when backoff state is unavailable")

    runner = EngineRunner(
        EngineConfig(schema_version=1, sources=(), playlists=()),
        store,
        auth,
        client_factory=client_factory,
        clock=lambda: NOW,
    )

    def fail_select(_connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runner.rate_limit_journal, "_select_row", fail_select)

    result = runner.run_cycle()

    assert not result.ok
    assert result.error == "Spotify rate-limit state is unavailable"
    assert auth.calls == 0
    assert client_factory_calls == 0


def test_retry_after_deadline_starts_when_429_is_received(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = SpotifyRateLimitJournal(store)
    current_time = [NOW]
    delegate = _Slow429(current_time)
    client = SpotifyRateLimitGuardClient(
        delegate,
        journal,
        clock=lambda: current_time[0],
    )

    with pytest.raises(SpotifyApiError, match="429"):
        client.show_episodes("slow-show")

    state = journal.get()
    assert delegate.calls == 1
    assert state is not None
    assert state.observed_at == NOW + timedelta(seconds=20)
    assert state.retry_not_before == NOW + timedelta(seconds=50)
    assert state.retry_after_seconds == 30
    assert state.backoff_source == "spotify_header"
