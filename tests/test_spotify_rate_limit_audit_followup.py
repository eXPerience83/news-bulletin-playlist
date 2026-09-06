from __future__ import annotations

import io
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyClient
from news_bulletin_playlist.spotify_backoff import (
    DEFAULT_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS,
    SpotifyRateLimitGuardClient,
    SpotifyRateLimitJournal,
    SpotifyRateLimitSuppressed,
)

NOW = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)


class _BlockingCatalogue:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.entered = entered
        self.release = release
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
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise AssertionError("timed out waiting to release first Spotify delegate")
        return {"items": [], "next": None}


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
        raise SpotifyApiError(429, "rate limited", retry_after=1800)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    return store


def test_shared_request_gate_prevents_cross_runner_delegate_race(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_journal = SpotifyRateLimitJournal(store)
    second_journal = SpotifyRateLimitJournal(SQLiteStore(store.path))
    assert first_journal._request_lock is second_journal._request_lock

    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_clock_called = threading.Event()
    first_delegate = _BlockingCatalogue(first_entered, release_first)
    second_delegate = _RateLimitedCatalogue()
    first_client = SpotifyRateLimitGuardClient(first_delegate, first_journal, clock=lambda: NOW)

    def second_clock() -> datetime:
        second_clock_called.set()
        return NOW

    second_client = SpotifyRateLimitGuardClient(
        second_delegate,
        second_journal,
        clock=second_clock,
    )
    first_errors: list[BaseException] = []
    second_errors: list[BaseException] = []

    def run_first() -> None:
        try:
            first_client.show_episodes("first-show")
        except BaseException as exc:  # pragma: no cover - asserted below
            first_errors.append(exc)

    def run_second() -> None:
        second_started.set()
        try:
            second_client.show_episodes("second-show")
        except BaseException as exc:  # pragma: no cover - asserted below
            second_errors.append(exc)

    first_thread = threading.Thread(target=run_first)
    second_thread = threading.Thread(target=run_second)
    first_thread.start()
    assert first_entered.wait(timeout=2)
    second_thread.start()
    assert second_started.wait(timeout=2)

    # The second wrapper must block before even reading its clock/backoff state while the first
    # wrapper owns the installation-wide request gate around its delegate call.
    assert not second_clock_called.wait(timeout=0.25)
    assert second_delegate.calls == 0

    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert first_errors == []
    assert len(second_errors) == 1
    assert isinstance(second_errors[0], SpotifyApiError)
    assert second_delegate.calls == 1

    state = SpotifyRateLimitJournal(store).active(now=NOW + timedelta(seconds=1))
    assert state is not None
    assert state.retry_not_before == NOW + timedelta(seconds=1800)

    with pytest.raises(SpotifyRateLimitSuppressed):
        first_client.show_episodes("must-not-reach-first-delegate")
    assert first_delegate.calls == 1


def test_noncanonical_persisted_timestamp_is_rejected_before_expiry_comparison(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    journal = SpotifyRateLimitJournal(store)
    journal.activate(
        observed_at=NOW - timedelta(seconds=1),
        retry_after_seconds=1,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE spotify_rate_limit_backoff SET retry_not_before = ? WHERE singleton = 1",
            ("2026-09-06T08:00:00Z",),
        )

    with pytest.raises(PersistenceError, match="invalid timestamp"):
        journal.get()


@pytest.mark.parametrize("header", ["+1", "1_0", "-0"])
def test_malformed_numeric_retry_after_uses_conservative_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    header: str,
) -> None:
    store = _store(tmp_path)

    def fake_urlopen(request: object, *, timeout: float) -> object:
        assert isinstance(request, urllib.request.Request)
        assert timeout == 30.0
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "rate limited",
            {"Retry-After": header},
            io.BytesIO(b"provider-body-must-not-escape"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = SpotifyRateLimitGuardClient(
        SpotifyClient("token-sentinel"),
        SpotifyRateLimitJournal(store),
        clock=lambda: NOW,
    )

    with pytest.raises(SpotifyApiError) as error:
        client.show_episodes("show-id")

    assert error.value.retry_after is None
    state = SpotifyRateLimitJournal(store).get()
    assert state is not None
    assert state.backoff_source == "fallback"
    assert state.retry_after_seconds is None
    assert state.retry_not_before == NOW + timedelta(
        seconds=DEFAULT_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS
    )


def test_oversized_decimal_retry_after_uses_fallback_and_latches_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    access_token = "oversized-header-token-sentinel"
    provider_body = b"oversized-header-provider-body-sentinel"
    calls = 0

    def fake_urlopen(request: object, *, timeout: float) -> object:
        nonlocal calls
        assert isinstance(request, urllib.request.Request)
        assert timeout == 30.0
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "rate limited",
            {"Retry-After": "9" * 10_000},
            io.BytesIO(provider_body),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = SpotifyRateLimitGuardClient(
        SpotifyClient(access_token),
        SpotifyRateLimitJournal(store),
        clock=lambda: NOW,
    )

    with pytest.raises(SpotifyApiError) as error:
        client.show_episodes("show-id")

    assert error.value.status == 429
    assert error.value.retry_after is None
    assert access_token not in str(error.value)
    assert provider_body.decode() not in str(error.value)

    state = SpotifyRateLimitJournal(store).get()
    assert state is not None
    assert state.backoff_source == "fallback"
    assert state.retry_after_seconds is None
    assert state.retry_not_before == NOW + timedelta(
        seconds=DEFAULT_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS
    )

    with pytest.raises(SpotifyRateLimitSuppressed):
        client.show_episodes("must-not-reach-provider")
    assert calls == 1

    events = DiagnosticEventStore(store.path).list_events(limit=20)
    rendered_events = repr(events)
    assert access_token not in rendered_events
    assert provider_body.decode() not in rendered_events
