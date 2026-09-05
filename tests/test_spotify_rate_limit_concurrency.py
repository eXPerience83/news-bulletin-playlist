from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    return store


def test_concurrent_activations_cannot_shorten_later_deadline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    barrier = threading.Barrier(3)
    failures: list[BaseException] = []

    def activate(seconds: int) -> None:
        try:
            journal = SpotifyRateLimitJournal(SQLiteStore(store.path))
            barrier.wait(timeout=2)
            journal.activate(observed_at=NOW, retry_after_seconds=seconds)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    long_thread = threading.Thread(target=activate, args=(3600,))
    short_thread = threading.Thread(target=activate, args=(30,))
    long_thread.start()
    short_thread.start()
    barrier.wait(timeout=2)
    long_thread.join(timeout=2)
    short_thread.join(timeout=2)

    assert not long_thread.is_alive()
    assert not short_thread.is_alive()
    assert failures == []
    state = SpotifyRateLimitJournal(store).get()
    assert state is not None
    assert state.retry_not_before == NOW + timedelta(hours=1)
    assert state.retry_after_seconds == 3600
    assert state.backoff_source == "spotify_header"


def test_sub_millisecond_extension_is_not_lost(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = SpotifyRateLimitJournal(store)

    first = journal.activate(observed_at=NOW, retry_after_seconds=1)
    extended = journal.activate(
        observed_at=NOW + timedelta(microseconds=500),
        retry_after_seconds=1,
    )

    assert first.retry_not_before == NOW + timedelta(seconds=1)
    assert extended.retry_not_before == NOW + timedelta(seconds=1, microseconds=500)
    assert SpotifyRateLimitJournal(store).get() == extended


def test_expiry_cannot_delete_concurrently_extended_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    seed = SpotifyRateLimitJournal(store)
    seed.activate(
        observed_at=NOW - timedelta(minutes=10),
        retry_after_seconds=60,
    )
    stale = seed.get()
    assert stale is not None
    assert stale.retry_not_before < NOW

    reader = SpotifyRateLimitJournal(SQLiteStore(store.path))
    writer = SpotifyRateLimitJournal(SQLiteStore(store.path))
    original_get = reader.get
    calls = 0

    def stale_then_current():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            writer.activate(observed_at=NOW, retry_after_seconds=300)
            return stale
        return original_get()

    monkeypatch.setattr(reader, "get", stale_then_current)

    active = reader.active(now=NOW)

    assert active is not None
    assert active.retry_not_before == NOW + timedelta(minutes=5)
    persisted = SpotifyRateLimitJournal(store).get()
    assert persisted == active


def test_expiry_preserves_sub_millisecond_concurrent_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    seed = SpotifyRateLimitJournal(store)
    seed.activate(
        observed_at=NOW - timedelta(seconds=2),
        retry_after_seconds=1,
    )
    stale = seed.get()
    assert stale is not None
    assert stale.retry_not_before < NOW

    reader = SpotifyRateLimitJournal(SQLiteStore(store.path))
    writer = SpotifyRateLimitJournal(SQLiteStore(store.path))
    original_get = reader.get
    calls = 0

    def stale_then_current():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            writer.activate(
                observed_at=NOW - timedelta(microseconds=999_900),
                retry_after_seconds=1,
            )
            return stale
        return original_get()

    monkeypatch.setattr(reader, "get", stale_then_current)

    active = reader.active(now=NOW)

    assert active is not None
    assert active.retry_not_before == NOW + timedelta(microseconds=100)
    persisted = SpotifyRateLimitJournal(store).get()
    assert persisted == active
