from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from news_bulletin_playlist.collection import (
    FeedFetcher,
    collect_required_sources,
    fetch_feed,
    required_sources,
)
from news_bulletin_playlist.desired_state import DesiredStateError
from news_bulletin_playlist.models import EngineConfig, PlaylistId, SourceId
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.reconciliation import (
    SpotifyReconciliationError,
    build_desired_state_from_store,
    reconcile_spotify_playlist,
)
from news_bulletin_playlist.spotify.auth import SpotifyAuthError
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)
from news_bulletin_playlist.spotify.matcher import (
    MatchConfigurationError,
    MatchResponseError,
    match_source_editions,
)

DEFAULT_ENGINE_INTERVAL = timedelta(minutes=10)


class SpotifyAuthProvider(Protocol):
    def get_access_token(self, *, now: datetime | None = None) -> str: ...


class SpotifyEngineClient(Protocol):
    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]: ...

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]: ...

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]: ...


class EngineCycleRunner(Protocol):
    def run_cycle(self) -> EngineCycleResult: ...


SpotifyClientFactory = Callable[[str], SpotifyEngineClient]
Clock = Callable[[], datetime]


class EngineCycleAlreadyRunning(RuntimeError):
    """Raised when a second cycle is requested while one is still executing."""


@dataclass(frozen=True, slots=True)
class SourceCycleOutcome:
    source_id: SourceId
    collection_ok: bool
    matching_ok: bool | None
    edition_count: int
    matched_count: int
    last_success_at: datetime | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.collection_ok and self.matching_ok is not False


@dataclass(frozen=True, slots=True)
class PlaylistCycleOutcome:
    playlist_id: PlaylistId
    ok: bool
    desired_count: int
    applied_count: int | None
    wrote: bool | None
    last_success_at: datetime | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EngineCycleResult:
    started_at: datetime
    finished_at: datetime
    ok: bool
    sources: tuple[SourceCycleOutcome, ...]
    playlists: tuple[PlaylistCycleOutcome, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class OperationalStatusSnapshot:
    configured: bool
    running: bool
    next_run_at: datetime | None
    last_cycle: EngineCycleResult | None


class OperationalStatus:
    """Thread-safe in-memory status shared by the scheduler and read-only Web UI."""

    def __init__(self, *, configured: bool) -> None:
        self._configured = configured
        self._running = False
        self._next_run_at: datetime | None = None
        self._last_cycle: EngineCycleResult | None = None
        self._lock = threading.Lock()

    def begin_cycle(self) -> None:
        with self._lock:
            self._running = True
            self._next_run_at = None

    def finish_cycle(
        self,
        result: EngineCycleResult,
        *,
        next_run_at: datetime | None,
    ) -> None:
        with self._lock:
            self._running = False
            self._last_cycle = result
            self._next_run_at = next_run_at

    def set_next_run(self, value: datetime | None) -> None:
        with self._lock:
            self._next_run_at = value

    def snapshot(self) -> OperationalStatusSnapshot:
        with self._lock:
            return OperationalStatusSnapshot(
                configured=self._configured,
                running=self._running,
                next_run_at=self._next_run_at,
                last_cycle=self._last_cycle,
            )


class EngineRunner:
    """Execute one complete fetch-once, persist-once, multi-playlist production cycle."""

    def __init__(
        self,
        config: EngineConfig,
        store: SQLiteStore,
        auth: SpotifyAuthProvider,
        *,
        fetcher: FeedFetcher = fetch_feed,
        client_factory: SpotifyClientFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.auth = auth
        self.fetcher = fetcher
        self.client_factory = client_factory or _spotify_client
        self.clock = clock or _utc_now
        self._cycle_lock = threading.Lock()

    def run_cycle(self) -> EngineCycleResult:
        if not self._cycle_lock.acquire(blocking=False):
            raise EngineCycleAlreadyRunning("engine cycle is already running")
        try:
            return self._run_cycle()
        finally:
            self._cycle_lock.release()

    def _run_cycle(self) -> EngineCycleResult:
        started_at = _as_utc(self.clock())
        source_outcomes: dict[SourceId, SourceCycleOutcome] = {}
        playlist_outcomes: list[PlaylistCycleOutcome] = []
        unsafe_source_ids: set[SourceId] = set()

        try:
            collection = collect_required_sources(self.config, fetcher=self.fetcher)
            collected_at = _as_utc(self.clock())
            for result in collection.sources:
                previous_source_state = self.store.get_source_state(result.source_id)
                if not result.ok and (
                    previous_source_state is None
                    or previous_source_state.last_success_at is None
                ):
                    unsafe_source_ids.add(result.source_id)
                if result.ok:
                    self.store.upsert_editions(result.editions, observed_at=collected_at)
                self.store.record_source_run(
                    result.source_id,
                    started_at=started_at,
                    finished_at=collected_at,
                    ok=result.ok,
                    edition_count=len(result.editions),
                    error=result.error,
                )
                source_state = self.store.get_source_state(result.source_id)
                source_outcomes[result.source_id] = SourceCycleOutcome(
                    source_id=result.source_id,
                    collection_ok=result.ok,
                    matching_ok=None,
                    edition_count=len(result.editions),
                    matched_count=0,
                    last_success_at=(
                        None if source_state is None else source_state.last_success_at
                    ),
                    error=result.error,
                )
        except PersistenceError as exc:
            return self._fatal_result(started_at, source_outcomes, playlist_outcomes, exc)

        try:
            access_token = self.auth.get_access_token(now=_as_utc(self.clock()))
        except SpotifyAuthError as exc:
            return self._authorization_failure(
                started_at,
                source_outcomes,
                playlist_outcomes,
                exc,
            )

        client = self.client_factory(access_token)
        matching_now = _as_utc(self.clock())
        matching_cutoff = self._matching_cutoff(matching_now)
        for source in required_sources(self.config):
            had_durable_match = False
            try:
                editions = tuple(
                    edition
                    for edition in self.store.list_editions(source_id=source.id)
                    if matching_cutoff <= edition.published_at <= matching_now
                )
                had_durable_match = any(
                    self.store.get_spotify_episode_uri(
                        edition.source_id,
                        edition.source_native_id,
                    )
                    is not None
                    for edition in editions
                )
                if editions:
                    match_result = match_source_editions(
                        client,
                        self.store,
                        source,
                        editions,
                        now=matching_now,
                    )
                    matched_count = sum(
                        outcome.spotify_episode_uri is not None
                        for outcome in match_result.outcomes
                    )
                else:
                    matched_count = 0
                current = source_outcomes.get(source.id)
                if current is not None:
                    source_outcomes[source.id] = replace(
                        current,
                        matching_ok=True,
                        matched_count=matched_count,
                    )
            except (
                MatchConfigurationError,
                MatchResponseError,
                SpotifyApiError,
                SpotifyTransportError,
            ) as exc:
                if not had_durable_match:
                    unsafe_source_ids.add(source.id)
                current = source_outcomes.get(source.id)
                if current is not None:
                    source_outcomes[source.id] = replace(
                        current,
                        matching_ok=False,
                        error=_combine_errors(current.error, str(exc)),
                    )
            except PersistenceError as exc:
                return self._fatal_result(started_at, source_outcomes, playlist_outcomes, exc)

        for playlist in self.config.playlists:
            if not playlist.enabled:
                continue
            playlist_started_at = _as_utc(self.clock())
            desired_count = 0
            unsafe_for_playlist = tuple(
                source_id
                for source_id in playlist.source_selection.explicit
                if source_id in unsafe_source_ids
            )
            if unsafe_for_playlist:
                try:
                    desired = build_desired_state_from_store(
                        self.store,
                        playlist,
                        now=playlist_started_at,
                    )
                    desired_count = len(desired.items)
                    reason = (
                        "destination preserved because no last-known-good state is available "
                        "for source(s): "
                        + ", ".join(str(source_id) for source_id in unsafe_for_playlist)
                    )
                    playlist_finished_at = _as_utc(self.clock())
                    self.store.record_playlist_run(
                        playlist.id,
                        started_at=playlist_started_at,
                        finished_at=playlist_finished_at,
                        ok=False,
                        desired_count=desired_count,
                        applied_count=0,
                        error=reason,
                    )
                    playlist_state = self.store.get_playlist_state(playlist.id)
                except PersistenceError as exc:
                    return self._fatal_result(
                        started_at,
                        source_outcomes,
                        playlist_outcomes,
                        exc,
                    )
                playlist_outcomes.append(
                    PlaylistCycleOutcome(
                        playlist_id=playlist.id,
                        ok=False,
                        desired_count=desired_count,
                        applied_count=None,
                        wrote=None,
                        last_success_at=(
                            None if playlist_state is None else playlist_state.last_success_at
                        ),
                        error=reason,
                    )
                )
                continue

            try:
                desired = build_desired_state_from_store(
                    self.store,
                    playlist,
                    now=playlist_started_at,
                )
                desired_count = len(desired.items)
                reconciled = reconcile_spotify_playlist(
                    client,
                    playlist,
                    desired,
                    store=self.store,
                )
                playlist_finished_at = _as_utc(self.clock())
                self.store.record_playlist_run(
                    playlist.id,
                    started_at=playlist_started_at,
                    finished_at=playlist_finished_at,
                    ok=True,
                    desired_count=reconciled.desired_count,
                    applied_count=reconciled.applied_count or 0,
                )
                playlist_state = self.store.get_playlist_state(playlist.id)
                playlist_outcomes.append(
                    PlaylistCycleOutcome(
                        playlist_id=playlist.id,
                        ok=True,
                        desired_count=reconciled.desired_count,
                        applied_count=reconciled.applied_count,
                        wrote=reconciled.wrote,
                        last_success_at=(
                            None if playlist_state is None else playlist_state.last_success_at
                        ),
                        error=reconciled.warning,
                    )
                )
            except (
                DesiredStateError,
                SpotifyApiError,
                SpotifyTransportError,
                SpotifyReconciliationError,
            ) as exc:
                playlist_finished_at = _as_utc(self.clock())
                try:
                    self.store.record_playlist_run(
                        playlist.id,
                        started_at=playlist_started_at,
                        finished_at=playlist_finished_at,
                        ok=False,
                        desired_count=desired_count,
                        applied_count=0,
                        error=str(exc),
                    )
                    playlist_state = self.store.get_playlist_state(playlist.id)
                except PersistenceError as persistence_exc:
                    return self._fatal_result(
                        started_at,
                        source_outcomes,
                        playlist_outcomes,
                        persistence_exc,
                    )
                playlist_outcomes.append(
                    PlaylistCycleOutcome(
                        playlist_id=playlist.id,
                        ok=False,
                        desired_count=desired_count,
                        applied_count=None,
                        wrote=None,
                        last_success_at=(
                            None if playlist_state is None else playlist_state.last_success_at
                        ),
                        error=str(exc),
                    )
                )
            except PersistenceError as exc:
                return self._fatal_result(started_at, source_outcomes, playlist_outcomes, exc)

        finished_at = _as_utc(self.clock())
        try:
            self.store.prune_operational_history(
                now=finished_at,
                protected_identities=self._protected_identities(finished_at),
            )
        except PersistenceError as exc:
            return self._fatal_result(started_at, source_outcomes, playlist_outcomes, exc)

        sources = tuple(
            source_outcomes[source.id]
            for source in required_sources(self.config)
            if source.id in source_outcomes
        )
        playlists = tuple(playlist_outcomes)
        failed_sources = sum(not outcome.ok for outcome in sources)
        failed_playlists = sum(not outcome.ok for outcome in playlists)
        ok = failed_sources == 0 and failed_playlists == 0
        error = None
        if not ok:
            error = (
                f"cycle completed with {failed_sources} source failure(s) and "
                f"{failed_playlists} playlist failure(s)"
            )
        return EngineCycleResult(
            started_at=started_at,
            finished_at=finished_at,
            ok=ok,
            sources=sources,
            playlists=playlists,
            error=error,
        )

    def _authorization_failure(
        self,
        started_at: datetime,
        source_outcomes: dict[SourceId, SourceCycleOutcome],
        playlist_outcomes: list[PlaylistCycleOutcome],
        error: SpotifyAuthError,
    ) -> EngineCycleResult:
        message = str(error)
        for playlist in self.config.playlists:
            if not playlist.enabled:
                continue
            attempt_at = _as_utc(self.clock())
            desired_count = 0
            try:
                desired = build_desired_state_from_store(self.store, playlist, now=attempt_at)
                desired_count = len(desired.items)
                self.store.record_playlist_run(
                    playlist.id,
                    started_at=attempt_at,
                    finished_at=attempt_at,
                    ok=False,
                    desired_count=desired_count,
                    applied_count=0,
                    error=message,
                )
                playlist_state = self.store.get_playlist_state(playlist.id)
            except PersistenceError as exc:
                return self._fatal_result(started_at, source_outcomes, playlist_outcomes, exc)
            playlist_outcomes.append(
                PlaylistCycleOutcome(
                    playlist_id=playlist.id,
                    ok=False,
                    desired_count=desired_count,
                    applied_count=None,
                    wrote=None,
                    last_success_at=(
                        None if playlist_state is None else playlist_state.last_success_at
                    ),
                    error=message,
                )
            )

        return EngineCycleResult(
            started_at=started_at,
            finished_at=_as_utc(self.clock()),
            ok=False,
            sources=tuple(
                source_outcomes[source.id]
                for source in required_sources(self.config)
                if source.id in source_outcomes
            ),
            playlists=tuple(playlist_outcomes),
            error=message,
        )

    def _fatal_result(
        self,
        started_at: datetime,
        source_outcomes: dict[SourceId, SourceCycleOutcome],
        playlist_outcomes: list[PlaylistCycleOutcome],
        error: PersistenceError,
    ) -> EngineCycleResult:
        return EngineCycleResult(
            started_at=started_at,
            finished_at=_as_utc(self.clock()),
            ok=False,
            sources=tuple(source_outcomes.values()),
            playlists=tuple(playlist_outcomes),
            error=str(error),
        )

    def _matching_cutoff(self, now: datetime) -> datetime:
        enabled = [
            playlist.retention_hours
            for playlist in self.config.playlists
            if playlist.enabled
        ]
        retention_hours = max(enabled, default=48)
        return now - timedelta(hours=retention_hours)

    def _protected_identities(self, now: datetime) -> set[tuple[SourceId, str]]:
        """Protect canonical state still eligible for any configured playlist window."""
        protected: set[tuple[SourceId, str]] = set()
        for playlist in self.config.playlists:
            if not playlist.enabled:
                continue
            cutoff = now - timedelta(hours=playlist.retention_hours)
            for source_id in playlist.source_selection.explicit:
                for edition in self.store.list_editions(source_id=source_id):
                    if edition.published_at < cutoff:
                        break
                    if edition.published_at <= now:
                        protected.add(edition.identity)
        return protected


class EngineScheduler:
    """Run one engine sequentially at a bounded cadence inside the durable process."""

    def __init__(
        self,
        runner: EngineCycleRunner,
        status: OperationalStatus,
        *,
        interval: timedelta = DEFAULT_ENGINE_INTERVAL,
        clock: Clock | None = None,
    ) -> None:
        if interval <= timedelta(0):
            raise ValueError("engine interval must be positive")
        self.runner = runner
        self.status = status
        self.interval = interval
        self.clock = clock or _utc_now
        self._wake_event = threading.Event()

    def wake(self) -> None:
        """Request an early next cycle, for example immediately after successful OAuth."""
        self._wake_event.set()

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.status.begin_cycle()
            try:
                result = self.runner.run_cycle()
            except Exception as exc:  # noqa: BLE001 - keep the long-lived scheduler alive safely
                observed_at = _as_utc(self.clock())
                result = EngineCycleResult(
                    started_at=observed_at,
                    finished_at=observed_at,
                    ok=False,
                    sources=(),
                    playlists=(),
                    error=f"unexpected engine failure: {type(exc).__name__}",
                )

            if stop_event.is_set():
                self.status.finish_cycle(result, next_run_at=None)
                break

            next_run_at = _as_utc(self.clock()) + self.interval
            self.status.finish_cycle(result, next_run_at=next_run_at)
            self._wait_for_wake_or_stop(stop_event)

        self.status.set_next_run(None)

    def _wait_for_wake_or_stop(self, stop_event: threading.Event) -> None:
        """Wait for cadence or wake while keeping external stop requests responsive."""
        deadline = time.monotonic() + self.interval.total_seconds()
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._wake_event.wait(min(remaining, 0.25)):
                self._wake_event.clear()
                return


def _spotify_client(access_token: str) -> SpotifyEngineClient:
    return SpotifyClient(access_token=access_token)


def _combine_errors(first: str | None, second: str) -> str:
    return second if first is None else f"{first}; {second}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("engine timestamps must be timezone-aware")
    return value.astimezone(UTC)
