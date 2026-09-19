from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import EngineConfig
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.spotify.client import SpotifyApiError
from news_bulletin_playlist.spotify_backoff import (
    SpotifyRateLimitBackoffActive,
    SpotifyRateLimitGuardAuth,
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


def test_failed_backoff_persistence_survives_repeated_failure_then_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    journal = SpotifyRateLimitJournal(store)
    current_time = [NOW]
    delegate = _Slow429(current_time)
    client = SpotifyRateLimitGuardClient(
        delegate,
        journal,
        clock=lambda: current_time[0],
    )
    original_select = journal._select_row
    select_calls = 0

    def fail_initial_persist_select(connection: sqlite3.Connection):  # type: ignore[no-untyped-def]
        nonlocal select_calls
        select_calls += 1
        if select_calls == 2:
            raise sqlite3.OperationalError("database is locked")
        return original_select(connection)

    monkeypatch.setattr(journal, "_select_row", fail_initial_persist_select)

    with pytest.raises(PersistenceError, match="persist Spotify rate-limit backoff"):
        client.show_episodes("slow-show")
    assert delegate.calls == 1
    assert SpotifyRateLimitJournal(store).get() is None

    auth = _Auth()
    repeated_failure_journal = SpotifyRateLimitJournal(store)

    def fail_re_persist(_candidate: object) -> None:
        raise PersistenceError("database is still locked")

    monkeypatch.setattr(repeated_failure_journal, "_persist_candidate", fail_re_persist)
    repeated_failure_auth = SpotifyRateLimitGuardAuth(
        auth,
        repeated_failure_journal,
        clock=lambda: NOW + timedelta(seconds=21),
    )

    with pytest.raises(SpotifyRateLimitBackoffActive, match="backoff active until"):
        repeated_failure_auth.get_access_token(now=NOW + timedelta(seconds=21))

    assert auth.calls == 0
    fallback = repeated_failure_journal._fallback_snapshot()
    assert fallback is not None
    assert fallback.observed_at == NOW + timedelta(seconds=20)
    assert fallback.retry_not_before == NOW + timedelta(seconds=50)
    assert SpotifyRateLimitJournal(store).get() is None

    recovered_journal = SpotifyRateLimitJournal(store)
    recovered_auth = SpotifyRateLimitGuardAuth(
        auth,
        recovered_journal,
        clock=lambda: NOW + timedelta(seconds=22),
    )
    with pytest.raises(SpotifyRateLimitBackoffActive):
        recovered_auth.get_access_token(now=NOW + timedelta(seconds=22))

    assert auth.calls == 0
    persisted = SpotifyRateLimitJournal(store).get()
    assert persisted is not None
    assert persisted.observed_at == NOW + timedelta(seconds=20)
    assert persisted.retry_not_before == NOW + timedelta(seconds=50)
    assert recovered_journal._fallback_snapshot() is None
