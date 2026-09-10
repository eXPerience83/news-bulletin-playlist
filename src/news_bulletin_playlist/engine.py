from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from news_bulletin_playlist.collection import (
    FeedFetcher,
    collect_required_sources,
    fetch_feed,
    required_sources,
)
from news_bulletin_playlist.desired_state import (
    DesiredPlaylistState,
    DesiredStateError,
    DurationEligibilityDecision,
)
from news_bulletin_playlist.models import (
    CanonicalEdition,
    EngineConfig,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.persistence import MatchStatus, PersistenceError, SQLiteStore
from news_bulletin_playlist.reconciliation import (
    MutationOutcome,
    ReconciliationEvidence,
    SpotifyReconciliationError,
    build_desired_state_from_store,
    reconcile_spotify_playlist,
)
from news_bulletin_playlist.snapshot_pending import PendingSnapshotJournal
from news_bulletin_playlist.spotify.auth import SpotifyAuthError
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)
from news_bulletin_playlist.spotify.matcher import (
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_SIZE,
    MatchConfigurationError,
    MatchEvaluationIntent,
    MatchResponseError,
    SourceMatchResult,
    SpotifyCatalogueResult,
    fetch_show_catalogue,
    match_source_editions,
    spotify_show_id,
)
from news_bulletin_playlist.spotify.uris import is_spotify_episode_uri
from news_bulletin_playlist.spotify_backoff import (
    SPOTIFY_RATE_LIMIT_MATCHING_DEFERRED_MESSAGE,
    SPOTIFY_RATE_LIMIT_PRESERVED_MESSAGE,
    SpotifyBackoffAuthProvider,
    SpotifyBackoffClient,
    SpotifyRateLimitGuardAuth,
    SpotifyRateLimitGuardClient,
    SpotifyRateLimitJournal,
    is_spotify_rate_limit_suppressed,
)
from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetrics
from news_bulletin_playlist.spotify_planner import (
    CatalogueState,
    DestinationState,
    DestinationVerificationContext,
    PlannerReason,
    PlannerShadowResult,
    SpotifyPlannerJournal,
    catalogue_context_fingerprint,
    destination_context_fingerprint,
    matching_input_fingerprint,
    plan_shadow_cycle,
    playlist_config_fingerprint,
    stable_fingerprint,
    unresolved_planning_reason,
)

DEFAULT_ENGINE_INTERVAL = timedelta(minutes=10)
_CATALOGUE_CONTRACT_VERSION = 1


class SpotifyAuthProvider(SpotifyBackoffAuthProvider, Protocol):
    """Authentication surface required by the production engine."""


class SpotifyEngineClient(SpotifyBackoffClient, Protocol):
    """Combined Spotify catalogue/playlist surface required by the production engine."""


class EngineCycleRunner(Protocol):
    def run_cycle(self) -> EngineCycleResult: ...


SpotifyClientFactory = Callable[[str], SpotifyEngineClient]
Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
PlaylistSkipGuard = Callable[[PlaylistDefinition], bool]


class EngineCycleAlreadyRunning(RuntimeError):
    """Raised when a second cycle is requested while one is still executing."""


@dataclass(frozen=True, slots=True)
class _SourceMatchingPlan:
    evaluation_intents: Mapping[tuple[SourceId, str], MatchEvaluationIntent]
    catalogue_audit_due: bool
    conservative: bool = False

    @property
    def needs_catalogue(self) -> bool:
        return bool(self.evaluation_intents) or self.catalogue_audit_due or self.conservative


@dataclass(frozen=True, slots=True)
class _DestinationWorkPlan:
    needs_remote: bool
    state_available: bool = True


@dataclass(frozen=True, slots=True)
class _DestinationRuntimeLease:
    context: DestinationVerificationContext
    expires_at: float


class DestinationRuntimeLeaseRegistry:
    """Keep at most one exact-context destination verification lease per process."""

    def __init__(self) -> None:
        self._leases: dict[PlaylistId, _DestinationRuntimeLease] = {}
        self._lock = threading.Lock()

    def allows(
        self,
        playlist_id: PlaylistId,
        expected: DestinationVerificationContext,
        *,
        monotonic_now: float,
    ) -> bool:
        with self._lock:
            lease = self._leases.get(playlist_id)
            if lease is None:
                return False
            if lease.context != expected or monotonic_now >= lease.expires_at:
                self._leases.pop(playlist_id, None)
                return False
            return True

    def publish(
        self,
        playlist_id: PlaylistId,
        expected: DestinationVerificationContext,
        *,
        verified_monotonic: float,
    ) -> None:
        with self._lock:
            self._leases[playlist_id] = _DestinationRuntimeLease(
                expected,
                verified_monotonic + timedelta(hours=6).total_seconds(),
            )

    def revoke(self, playlist_id: PlaylistId) -> None:
        with self._lock:
            self._leases.pop(playlist_id, None)

    def revoke_unless(
        self, playlist_id: PlaylistId, expected: DestinationVerificationContext
    ) -> None:
        with self._lock:
            lease = self._leases.get(playlist_id)
            if lease is not None and lease.context != expected:
                self._leases.pop(playlist_id, None)


class _CycleCatalogueCoordinator:
    """Share one bounded show inspection across compatible consumers in a cycle."""

    def __init__(self, client: SpotifyEngineClient, auth_context: str) -> None:
        self.client = client
        self.auth_context = auth_context
        self._cache: dict[tuple[str, str, str, int, int, int], object] = {}

    def get(self, source: SourceDefinition) -> tuple[SpotifyCatalogueResult, int]:
        show_id = spotify_show_id(source)
        key = (
            show_id,
            self.auth_context,
            "user-token/account",
            DEFAULT_PAGE_SIZE,
            DEFAULT_MAX_PAGES,
            _CATALOGUE_CONTRACT_VERSION,
        )
        cached = self._cache.get(key)
        if isinstance(cached, Exception):
            raise cached
        if isinstance(cached, SpotifyCatalogueResult):
            return cached, 0
        try:
            result = fetch_show_catalogue(
                self.client,
                show_id,
                page_size=DEFAULT_PAGE_SIZE,
                max_pages=DEFAULT_MAX_PAGES,
            )
        except (MatchResponseError, SpotifyApiError, SpotifyTransportError) as exc:
            self._cache[key] = exc
            raise
        self._cache[key] = result
        return result, result.request_count


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
    duration_decisions: tuple[DurationEligibilityDecision, ...] = ()
    request_item_count: int | None = None
    request_payload_bytes: int | None = None
    valid_uri_count: int | None = None
    invalid_uri_count: int | None = None
    duplicate_count: int | None = None
    desired_fingerprint: str | None = None
    desired_changed_since_previous_attempt: bool | None = None
    consecutive_failure_count: int | None = None
    first_failure_at: datetime | None = None
    last_failure_at: datetime | None = None
    write_outcome: str | None = None


@dataclass(frozen=True, slots=True)
class EngineCycleResult:
    started_at: datetime
    finished_at: datetime
    ok: bool
    sources: tuple[SourceCycleOutcome, ...]
    playlists: tuple[PlaylistCycleOutcome, ...]
    error: str | None = None
    planner_shadow: PlannerShadowResult | None = None
    provider_work_planned: bool = False
    provider_session_created: bool = False
    destinations_skipped: int = 0
    destinations_remote: int = 0
    destinations_preserved: int = 0


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
        playlist_mutation_guard: (
            Callable[[PlaylistDefinition], AbstractContextManager[None]] | None
        ) = None,
        request_metrics: SpotifyRequestMetrics | None = None,
        local_skips_enabled: bool = True,
        destination_leases: DestinationRuntimeLeaseRegistry | None = None,
        monotonic_clock: MonotonicClock = time.monotonic,
        playlist_skip_guard: PlaylistSkipGuard | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.fetcher = fetcher
        self.clock = clock or _utc_now
        self.rate_limit_journal = SpotifyRateLimitJournal(store)
        self.request_metrics = request_metrics or SpotifyRequestMetrics(store.path)
        self.request_metrics.initialize()
        self.local_skips_enabled = local_skips_enabled
        self.destination_leases = destination_leases or DestinationRuntimeLeaseRegistry()
        self.monotonic_clock = monotonic_clock
        self.playlist_skip_guard = playlist_skip_guard
        self.planner_journal = SpotifyPlannerJournal(store)
        self._planner_auth_reader = getattr(auth, "planner_authorization_context", None)
        self._shadow_auth_context: str | None = None
        self.auth = SpotifyRateLimitGuardAuth(
            auth,
            self.rate_limit_journal,
            clock=self.clock,
        )
        base_client_factory = client_factory or partial(
            _spotify_client,
            metrics=self.request_metrics,
        )

        def guarded_client_factory(access_token: str) -> SpotifyEngineClient:
            return SpotifyRateLimitGuardClient(
                base_client_factory(access_token),
                self.rate_limit_journal,
                clock=self.clock,
            )

        self.client_factory = guarded_client_factory
        self.playlist_mutation_guard = playlist_mutation_guard
        self._cycle_lock = threading.Lock()
        # Operational diagnostics only: these observations intentionally reset on process restart.
        self._previous_desired_fingerprints: dict[PlaylistId, str] = {}
        self._playlist_failure_streaks: dict[PlaylistId, tuple[int, datetime]] = {}
        self._planner_shadow = PlannerShadowResult.unsafe(PlannerReason.MISSING_EVIDENCE)

    def run_cycle(self) -> EngineCycleResult:
        if not self._cycle_lock.acquire(blocking=False):
            raise EngineCycleAlreadyRunning("engine cycle is already running")
        try:
            return self._run_cycle()
        finally:
            self._cycle_lock.release()

    def _run_cycle(self) -> EngineCycleResult:
        started_at = _as_utc(self.clock())
        self._planner_shadow = PlannerShadowResult.unsafe(PlannerReason.MISSING_EVIDENCE)
        source_outcomes: dict[SourceId, SourceCycleOutcome] = {}
        playlist_outcomes: list[PlaylistCycleOutcome] = []
        unsafe_source_ids: set[SourceId] = set()

        try:
            collection = collect_required_sources(self.config, fetcher=self.fetcher)
            collected_at = _as_utc(self.clock())
            for result in collection.sources:
                previous_source_state = self.store.get_source_state(result.source_id)
                if not result.ok and (
                    previous_source_state is None or previous_source_state.last_success_at is None
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

        self._planner_shadow = self._compute_shadow_plan(collected_at)

        matching_now = _as_utc(self.clock())
        try:
            source_editions, source_plans = self._build_source_work(matching_now)
        except PersistenceError as exc:
            return self._fatal_result(started_at, source_outcomes, playlist_outcomes, exc)

        matching_work = any(
            plan.needs_catalogue and source_id not in unsafe_source_ids
            for source_id, plan in source_plans.items()
        )
        destination_work_ids, persistence_blocked = self._plan_destinations(
            matching_now, unsafe_source_ids
        )
        destination_work = bool(destination_work_ids)
        cannot_safely_use_provider = bool(persistence_blocked) and not destination_work
        if (
            not matching_work
            and not destination_work
            and (self.local_skips_enabled or cannot_safely_use_provider)
        ):
            return self._finish_local_only_cycle(
                started_at,
                source_outcomes,
                unsafe_source_ids,
                persistence_blocked,
                matching_now,
            )

        try:
            authorization_session = self.auth.acquire_provider_session(now=_as_utc(self.clock()))
        except SpotifyAuthError as exc:
            return self._authorization_failure(
                started_at,
                source_outcomes,
                playlist_outcomes,
                exc,
            )

        if authorization_session.authorization_context != self._shadow_auth_context:
            self._shadow_auth_context = authorization_session.authorization_context
            for playlist in self.config.playlists:
                self.destination_leases.revoke(playlist.id)
            try:
                source_editions, source_plans = self._build_source_work(matching_now)
            except PersistenceError as exc:
                return self._fatal_result(started_at, source_outcomes, playlist_outcomes, exc)
            destination_work_ids, persistence_blocked = self._plan_destinations(
                matching_now, unsafe_source_ids
            )

        client = self.client_factory(authorization_session.access_token)
        catalogue_coordinator = _CycleCatalogueCoordinator(
            client,
            authorization_session.authorization_context,
        )
        remote_destination_ids: set[PlaylistId] = set()
        for source in required_sources(self.config):
            had_durable_match = False
            try:
                editions = source_editions[source.id]
                had_durable_match = any(
                    self.store.get_spotify_episode_uri(
                        edition.source_id,
                        edition.source_native_id,
                    )
                    is not None
                    for edition in editions
                )
                plan = source_plans[source.id]
                catalogue = None
                catalogue_request_count = 0
                if plan.needs_catalogue:
                    catalogue, catalogue_request_count = catalogue_coordinator.get(source)
                if editions:
                    match_result = match_source_editions(
                        client,
                        self.store,
                        source,
                        editions,
                        now=matching_now,
                        catalogue=catalogue,
                        evaluation_intents=plan.evaluation_intents,
                        validate_cached=catalogue is not None,
                        catalogue_request_count=catalogue_request_count,
                    )
                    matched_count = sum(
                        outcome.spotify_episode_uri is not None for outcome in match_result.outcomes
                    )
                    self._observe_matching_result(source, match_result, matching_now)
                else:
                    matched_count = 0
                    if catalogue is not None:
                        self._observe_catalogue_inspection(source, matching_now)
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
                suppressed = is_spotify_rate_limit_suppressed(exc)
                if not had_durable_match:
                    unsafe_source_ids.add(source.id)
                current = source_outcomes.get(source.id)
                if current is not None:
                    source_outcomes[source.id] = replace(
                        current,
                        matching_ok=None if suppressed else False,
                        error=_combine_errors(
                            current.error,
                            SPOTIFY_RATE_LIMIT_MATCHING_DEFERRED_MESSAGE
                            if suppressed
                            else str(exc),
                        ),
                    )
            except PersistenceError as exc:
                return self._fatal_result(started_at, source_outcomes, playlist_outcomes, exc)

        for playlist in self.config.playlists:
            if not playlist.enabled:
                continue
            playlist_started_at = _as_utc(self.clock())
            desired_count = 0
            request_diagnostics: dict[str, Any] = {}
            unsafe_for_playlist = tuple(
                source_id
                for source_id in playlist.source_selection.explicit
                if source_id in unsafe_source_ids
            )
            if playlist.id in persistence_blocked:
                playlist_started_at = _as_utc(self.clock())
                error = "destination preserved because planner persistence is unavailable"
                self.store.record_playlist_run(
                    playlist.id,
                    started_at=playlist_started_at,
                    finished_at=_as_utc(self.clock()),
                    ok=False,
                    desired_count=0,
                    applied_count=0,
                    error=error,
                )
                playlist_outcomes.append(
                    PlaylistCycleOutcome(
                        playlist_id=playlist.id,
                        ok=False,
                        desired_count=0,
                        applied_count=None,
                        wrote=None,
                        last_success_at=None,
                        error=error,
                        write_outcome="not_attempted",
                    )
                )
                continue
            if unsafe_for_playlist:
                try:
                    try:
                        desired = build_desired_state_from_store(
                            self.store,
                            playlist,
                            now=playlist_started_at,
                            source_timezones=self._source_timezones(),
                        )
                        desired_count = len(desired.items)
                    except DesiredStateError:
                        desired_count = 0
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

            evidence = ReconciliationEvidence()
            verification_context = None
            try:
                desired = build_desired_state_from_store(
                    self.store,
                    playlist,
                    now=playlist_started_at,
                    source_timezones=self._source_timezones(),
                )
                desired_count = len(desired.items)
                request_diagnostics = self._request_diagnostics(playlist.id, desired.uris)
                verification_context = self._observe_desired_state(playlist, desired.uris)
                if verification_context is None:
                    self.destination_leases.revoke(playlist.id)
                    error = (
                        "destination preserved because final desired state could not be persisted"
                    )
                    self.store.record_playlist_run(
                        playlist.id,
                        started_at=playlist_started_at,
                        finished_at=_as_utc(self.clock()),
                        ok=False,
                        desired_count=desired_count,
                        applied_count=0,
                        error=error,
                    )
                    playlist_outcomes.append(
                        PlaylistCycleOutcome(
                            playlist_id=playlist.id,
                            ok=False,
                            desired_count=desired_count,
                            applied_count=None,
                            wrote=None,
                            last_success_at=None,
                            error=error,
                            write_outcome="not_attempted",
                        )
                    )
                    continue
                destination_plan = self._destination_work_plan(
                    playlist, playlist_started_at, verification_context
                )
                if not destination_plan.state_available:
                    error = "destination preserved because planner persistence is unavailable"
                    self.store.record_playlist_run(
                        playlist.id,
                        started_at=playlist_started_at,
                        finished_at=_as_utc(self.clock()),
                        ok=False,
                        desired_count=desired_count,
                        applied_count=0,
                        error=error,
                    )
                    playlist_outcomes.append(
                        PlaylistCycleOutcome(
                            playlist_id=playlist.id,
                            ok=False,
                            desired_count=desired_count,
                            applied_count=None,
                            wrote=None,
                            last_success_at=None,
                            error=error,
                            write_outcome="not_attempted",
                        )
                    )
                    continue
                if self.local_skips_enabled and not destination_plan.needs_remote:
                    if self._local_skip_still_authorized(playlist, verification_context):
                        playlist_finished_at = _as_utc(self.clock())
                        self.store.record_playlist_run(
                            playlist.id,
                            started_at=playlist_started_at,
                            finished_at=playlist_finished_at,
                            ok=True,
                            desired_count=desired_count,
                            applied_count=desired_count,
                        )
                        playlist_state = self.store.get_playlist_state(playlist.id)
                        playlist_outcomes.append(
                            PlaylistCycleOutcome(
                                playlist_id=playlist.id,
                                ok=True,
                                desired_count=desired_count,
                                applied_count=desired_count,
                                wrote=False,
                                last_success_at=(
                                    None
                                    if playlist_state is None
                                    else playlist_state.last_success_at
                                ),
                                duration_decisions=desired.duration_decisions,
                                **request_diagnostics,
                                write_outcome="skipped",
                            )
                        )
                    else:
                        error = "destination preserved because local skip authority changed"
                        self.store.record_playlist_run(
                            playlist.id,
                            started_at=playlist_started_at,
                            finished_at=_as_utc(self.clock()),
                            ok=False,
                            desired_count=desired_count,
                            applied_count=0,
                            error=error,
                        )
                        playlist_outcomes.append(
                            PlaylistCycleOutcome(
                                playlist_id=playlist.id,
                                ok=False,
                                desired_count=desired_count,
                                applied_count=None,
                                wrote=None,
                                last_success_at=None,
                                error=error,
                                duration_decisions=desired.duration_decisions,
                                **request_diagnostics,
                                write_outcome="not_attempted",
                            )
                        )
                    continue
                if not self._authorization_context_matches(
                    authorization_session.authorization_context
                ):
                    self.destination_leases.revoke(playlist.id)
                    raise SpotifyReconciliationError(
                        "Spotify authorization changed during cycle; destination preserved"
                    )
                self.destination_leases.revoke(playlist.id)
                self._begin_destination_verification_required(playlist.id)
                mutation_guard = partial(
                    self._provider_mutation_guard,
                    playlist,
                    authorization_session.authorization_context,
                )
                remote_destination_ids.add(playlist.id)
                reconciled = reconcile_spotify_playlist(
                    client,
                    playlist,
                    desired,
                    store=self.store,
                    mutation_guard=mutation_guard,
                    prepare_write=partial(
                        self._prepare_playlist_write,
                        playlist,
                        catalogue_coordinator,
                        playlist_started_at,
                    ),
                    prewrite_iteration_limit=(
                        sum(
                            len(self.store.list_editions(source_id=source_id))
                            for source_id in playlist.source_selection.explicit
                        )
                        + 1
                    ),
                    evidence=evidence,
                )
                desired = build_desired_state_from_store(
                    self.store,
                    playlist,
                    now=playlist_started_at,
                    source_timezones=self._source_timezones(),
                )
                desired_count = len(desired.items)
                request_diagnostics = self._request_diagnostics(playlist.id, desired.uris)
                verification_context = self._observe_desired_state(playlist, desired.uris)
                playlist_finished_at = _as_utc(self.clock())
                reconciled_fingerprint = (
                    None
                    if reconciled.verified_desired_uris is None
                    else stable_fingerprint(reconciled.verified_desired_uris)
                )
                confirmed = self._observe_destination_result(
                    playlist,
                    verified=(
                        reconciled.remote_evidence_qualified
                        and verification_context is not None
                        and verification_context.desired_fingerprint == reconciled_fingerprint
                        and self._authorization_context_matches(
                            authorization_session.authorization_context
                        )
                    ),
                    unknown=reconciled.mutation_outcome is MutationOutcome.UNKNOWN,
                    expected=verification_context,
                    observed_at=playlist_finished_at,
                )
                if confirmed and verification_context is not None:
                    self.destination_leases.publish(
                        playlist.id,
                        verification_context,
                        verified_monotonic=self.monotonic_clock(),
                    )
                else:
                    self.destination_leases.revoke(playlist.id)
                self.store.record_playlist_run(
                    playlist.id,
                    started_at=playlist_started_at,
                    finished_at=playlist_finished_at,
                    ok=True,
                    desired_count=reconciled.desired_count,
                    applied_count=reconciled.applied_count or 0,
                )
                playlist_state = self.store.get_playlist_state(playlist.id)
                self._playlist_failure_streaks.pop(playlist.id, None)
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
                        duration_decisions=desired.duration_decisions,
                        **request_diagnostics,
                        write_outcome=("verified" if reconciled.wrote else "not_attempted"),
                    )
                )
            except (
                DesiredStateError,
                SpotifyApiError,
                SpotifyTransportError,
                SpotifyReconciliationError,
            ) as exc:
                playlist_finished_at = _as_utc(self.clock())
                error_message = (
                    SPOTIFY_RATE_LIMIT_PRESERVED_MESSAGE
                    if is_spotify_rate_limit_suppressed(exc)
                    else str(exc)
                )
                self._observe_destination_result(
                    playlist,
                    verified=False,
                    unknown=evidence.mutation_outcome is MutationOutcome.UNKNOWN,
                    observed_at=playlist_finished_at,
                )
                self.destination_leases.revoke(playlist.id)
                try:
                    self.store.record_playlist_run(
                        playlist.id,
                        started_at=playlist_started_at,
                        finished_at=playlist_finished_at,
                        ok=False,
                        desired_count=desired_count,
                        applied_count=0,
                        error=error_message,
                    )
                    playlist_state = self.store.get_playlist_state(playlist.id)
                except PersistenceError as persistence_exc:
                    return self._fatal_result(
                        started_at,
                        source_outcomes,
                        playlist_outcomes,
                        persistence_exc,
                    )
                failure_count, first_failure_at = self._record_failure(
                    playlist.id, playlist_finished_at
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
                        error=error_message,
                        **request_diagnostics,
                        consecutive_failure_count=failure_count,
                        first_failure_at=first_failure_at,
                        last_failure_at=playlist_finished_at,
                        write_outcome=(
                            "unknown"
                            if evidence.mutation_outcome is MutationOutcome.UNKNOWN
                            else None
                        ),
                    )
                )
            except PersistenceError as exc:
                return self._fatal_result(started_at, source_outcomes, playlist_outcomes, exc)

        finished_at = _as_utc(self.clock())
        try:
            self._prune_operational_history(finished_at)
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
        cycle_error: str | None = None
        if not ok:
            cycle_error = (
                f"cycle completed with {failed_sources} source failure(s) and "
                f"{failed_playlists} playlist failure(s)"
            )
        return EngineCycleResult(
            started_at=started_at,
            finished_at=finished_at,
            ok=ok,
            sources=sources,
            playlists=playlists,
            error=cycle_error,
            planner_shadow=self._planner_shadow,
            provider_work_planned=True,
            provider_session_created=True,
            destinations_skipped=sum(outcome.write_outcome == "skipped" for outcome in playlists),
            destinations_remote=len(remote_destination_ids),
            destinations_preserved=sum(not outcome.ok for outcome in playlists),
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
                try:
                    desired = build_desired_state_from_store(
                        self.store,
                        playlist,
                        now=attempt_at,
                        source_timezones=self._source_timezones(),
                    )
                    desired_count = len(desired.items)
                except DesiredStateError:
                    desired_count = 0
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

        finished_at = _as_utc(self.clock())
        try:
            self._prune_operational_history(finished_at)
        except PersistenceError as exc:
            return self._fatal_result(started_at, source_outcomes, playlist_outcomes, exc)

        return EngineCycleResult(
            started_at=started_at,
            finished_at=finished_at,
            ok=False,
            sources=tuple(
                source_outcomes[source.id]
                for source in required_sources(self.config)
                if source.id in source_outcomes
            ),
            playlists=tuple(playlist_outcomes),
            error=message,
            planner_shadow=self._planner_shadow,
            provider_work_planned=True,
            provider_session_created=False,
            destinations_preserved=len(playlist_outcomes),
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
            planner_shadow=self._planner_shadow,
        )

    def _compute_shadow_plan(self, now: datetime) -> PlannerShadowResult:
        self._shadow_auth_context = None
        try:
            if self._planner_auth_reader is None:
                return PlannerShadowResult.unsafe(PlannerReason.MISSING_EVIDENCE)
            self._shadow_auth_context = self._planner_auth_reader()
            if self._shadow_auth_context is None:
                return PlannerShadowResult.unsafe(PlannerReason.MISSING_EVIDENCE)
            matching_cutoff = self._matching_cutoff(now)
            source_reasons: list[tuple[str, PlannerReason]] = []
            additional_reasons: set[PlannerReason] = set()
            catalogue_states: list[CatalogueState | None] = []
            for source in required_sources(self.config):
                context = catalogue_context_fingerprint(source, self._shadow_auth_context)
                state = self.planner_journal.get_catalogue(context)
                if state is None:
                    additional_reasons.add(PlannerReason.MISSING_EVIDENCE)
                elif state.contract_fingerprint != context:
                    additional_reasons.add(PlannerReason.INVALIDATED_CONTEXT)
                    state = None
                catalogue_states.append(state)
                for edition in self.store.list_editions(source_id=source.id):
                    if edition.published_at < matching_cutoff:
                        break
                    if edition.published_at > now:
                        continue
                    identity = f"{source.id!s}\0{edition.source_native_id}"
                    match = self.store.get_match_state(source.id, edition.source_native_id)
                    if match is None:
                        source_reasons.append((identity, PlannerReason.NEW_MATCH))
                        continue
                    fingerprint = matching_input_fingerprint(source, edition)
                    prior_input = self.planner_journal.get_match_input(
                        str(source.id), edition.source_native_id
                    )
                    if prior_input is None or prior_input.input_fingerprint != fingerprint:
                        source_reasons.append((identity, PlannerReason.INPUT_CHANGED))
                        continue
                    if match.status is MatchStatus.MATCHED:
                        if (
                            match.spotify_episode_uri is None
                            or not is_spotify_episode_uri(match.spotify_episode_uri)
                            or match.spotify_duration_seconds is None
                        ):
                            source_reasons.append((identity, PlannerReason.INPUT_CHANGED))
                        continue
                    retry = self.planner_journal.get_unresolved(
                        str(source.id), edition.source_native_id
                    )
                    retry_reason = unresolved_planning_reason(
                        retry, input_fingerprint=fingerprint, now=now
                    )
                    if retry_reason is PlannerReason.CLOCK_ROLLBACK:
                        additional_reasons.add(retry_reason)
                    elif retry_reason is not None:
                        source_reasons.append((identity, retry_reason))

            destinations: list[DestinationState] = []
            pending_count = 0
            auth_context = self._shadow_auth_context
            pending_journal = PendingSnapshotJournal(self.store)
            active_playlist_ids: list[str] = []
            for playlist in self.config.playlists:
                if not playlist.enabled:
                    continue
                active_playlist_ids.append(str(playlist.id))
                desired = build_desired_state_from_store(
                    self.store,
                    playlist,
                    now=now,
                    source_timezones=self._source_timezones(),
                )
                previous_destination = self.planner_journal.get_destination(str(playlist.id))
                destination_context = destination_context_fingerprint(playlist)
                config_fingerprint = playlist_config_fingerprint(playlist)
                if previous_destination is None:
                    additional_reasons.add(PlannerReason.MISSING_EVIDENCE)
                elif (
                    previous_destination.destination_context_fingerprint != destination_context
                    or previous_destination.config_fingerprint != config_fingerprint
                    or previous_destination.auth_context_fingerprint != auth_context
                ):
                    additional_reasons.add(PlannerReason.INVALIDATED_CONTEXT)
                destination_state = self.planner_journal.observe_destination(
                    playlist_id=str(playlist.id),
                    destination_context_fingerprint=destination_context,
                    config_fingerprint=config_fingerprint,
                    auth_context_fingerprint=auth_context,
                    desired_fingerprint=stable_fingerprint(desired.uris),
                )
                destinations.append(destination_state)
                if pending_journal.get(playlist.id) is not None:
                    pending_count += 1
            self.planner_journal.retain_contexts(
                catalogue_contexts=(
                    catalogue_context_fingerprint(source, auth_context)
                    for source in required_sources(self.config)
                ),
                playlist_ids=active_playlist_ids,
            )
            return plan_shadow_cycle(
                source_reasons=source_reasons,
                destination_states=destinations,
                catalogue_states=catalogue_states,
                pending_verification_count=pending_count,
                now=now,
                additional_reasons=additional_reasons,
            )
        except Exception:
            # Isolated shadow boundary: never mask the existing provider flow.
            return PlannerShadowResult.unsafe()

    def _destination_work_plan(
        self,
        playlist: PlaylistDefinition,
        now: datetime,
        expected: DestinationVerificationContext,
    ) -> _DestinationWorkPlan:
        try:
            state = self.planner_journal.get_destination(str(playlist.id))
            if state is None:
                return _DestinationWorkPlan(needs_remote=True)
            if (
                state.destination_context_fingerprint != expected.destination_context_fingerprint
                or state.config_fingerprint != expected.config_fingerprint
                or state.auth_context_fingerprint != expected.auth_context_fingerprint
                or state.observed_desired_fingerprint != expected.desired_fingerprint
                or state.confirmed_desired_fingerprint != expected.desired_fingerprint
                or state.dirty
                or state.unknown
                or state.last_verified_at is None
                or state.next_audit_at is None
                or now < state.last_verified_at
                or now >= state.next_audit_at
            ):
                return _DestinationWorkPlan(needs_remote=True)
            if PendingSnapshotJournal(self.store).get(playlist.id) is not None:
                self.destination_leases.revoke(playlist.id)
                return _DestinationWorkPlan(needs_remote=True)
            return _DestinationWorkPlan(
                needs_remote=not self.destination_leases.allows(
                    playlist.id,
                    expected,
                    monotonic_now=self.monotonic_clock(),
                )
            )
        except PersistenceError, ValueError:
            self.destination_leases.revoke(playlist.id)
            self._planner_shadow = PlannerShadowResult.unsafe()
            return _DestinationWorkPlan(needs_remote=False, state_available=False)

    def _begin_destination_verification_required(self, playlist_id: PlaylistId) -> None:
        """Durably remove skip authority before reconciliation can reach a mutation."""
        self.destination_leases.revoke(playlist_id)
        try:
            self.planner_journal.begin_destination_verification(str(playlist_id))
        except sqlite3.Error as exc:
            raise PersistenceError("cannot invalidate Spotify destination evidence") from exc

    def _finish_local_only_cycle(
        self,
        started_at: datetime,
        source_outcomes: dict[SourceId, SourceCycleOutcome],
        unsafe_source_ids: set[SourceId],
        persistence_blocked: set[PlaylistId],
        now: datetime,
    ) -> EngineCycleResult:
        playlist_outcomes: list[PlaylistCycleOutcome] = []
        try:
            for source in required_sources(self.config):
                current = source_outcomes.get(source.id)
                if current is None:
                    continue
                editions = self.store.list_editions(source_id=source.id)
                matched = sum(
                    self.store.get_spotify_episode_uri(edition.source_id, edition.source_native_id)
                    is not None
                    for edition in editions
                )
                source_outcomes[source.id] = replace(
                    current,
                    matching_ok=None if source.id in unsafe_source_ids else True,
                    matched_count=matched,
                )
            for playlist in self.config.playlists:
                if not playlist.enabled:
                    continue
                desired = build_desired_state_from_store(
                    self.store,
                    playlist,
                    now=now,
                    source_timezones=self._source_timezones(),
                )
                unsafe = tuple(
                    source_id
                    for source_id in playlist.source_selection.explicit
                    if source_id in unsafe_source_ids
                )
                error = None
                ok = not unsafe and playlist.id not in persistence_blocked
                if playlist.id in persistence_blocked:
                    error = "destination preserved because planner persistence is unavailable"
                elif unsafe:
                    error = (
                        "destination preserved because no last-known-good state is available "
                        "for source(s): " + ", ".join(str(value) for value in unsafe)
                    )
                elif not self._local_skip_still_authorized(
                    playlist,
                    self._destination_verification_context(playlist, desired.uris),
                ):
                    error = "destination preserved because local skip authority changed"
                    ok = False
                finished = _as_utc(self.clock())
                self.store.record_playlist_run(
                    playlist.id,
                    started_at=now,
                    finished_at=finished,
                    ok=ok,
                    desired_count=len(desired.items),
                    applied_count=0 if not ok else len(desired.items),
                    error=error,
                )
                state = self.store.get_playlist_state(playlist.id)
                playlist_outcomes.append(
                    PlaylistCycleOutcome(
                        playlist_id=playlist.id,
                        ok=ok,
                        desired_count=len(desired.items),
                        applied_count=None if not ok else len(desired.items),
                        wrote=None if not ok else False,
                        last_success_at=None if state is None else state.last_success_at,
                        error=error,
                        duration_decisions=desired.duration_decisions,
                        **self._request_diagnostics(playlist.id, desired.uris),
                        write_outcome="skipped" if ok else "not_attempted",
                    )
                )
            self._prune_operational_history(now)
        except (PersistenceError, DesiredStateError) as exc:
            if isinstance(exc, PersistenceError):
                return self._fatal_result(started_at, source_outcomes, playlist_outcomes, exc)
            return EngineCycleResult(
                started_at=started_at,
                finished_at=_as_utc(self.clock()),
                ok=False,
                sources=tuple(source_outcomes.values()),
                playlists=tuple(playlist_outcomes),
                error=str(exc),
                planner_shadow=self._planner_shadow,
            )
        sources = tuple(
            source_outcomes[source.id]
            for source in required_sources(self.config)
            if source.id in source_outcomes
        )
        ok = all(outcome.ok for outcome in sources) and all(
            outcome.ok for outcome in playlist_outcomes
        )
        return EngineCycleResult(
            started_at=started_at,
            finished_at=_as_utc(self.clock()),
            ok=ok,
            sources=sources,
            playlists=tuple(playlist_outcomes),
            error=None if ok else "cycle completed with local source failure(s)",
            planner_shadow=self._planner_shadow,
            provider_work_planned=False,
            provider_session_created=False,
            destinations_skipped=sum(outcome.ok for outcome in playlist_outcomes),
            destinations_remote=0,
            destinations_preserved=sum(not outcome.ok for outcome in playlist_outcomes),
        )

    def _source_matching_plan(
        self,
        source: SourceDefinition,
        editions: tuple[CanonicalEdition, ...],
        now: datetime,
    ) -> _SourceMatchingPlan:
        """Turn Phase-2 evidence into conservative Phase-3 catalogue work."""
        try:
            if self._shadow_auth_context is None:
                raise PersistenceError("Spotify planner authorization context unavailable")
            contract = catalogue_context_fingerprint(source, self._shadow_auth_context)
            catalogue = self.planner_journal.get_catalogue(contract)
            audit_due = (
                catalogue is None
                or catalogue.contract_fingerprint != contract
                or catalogue.last_verified_at is None
                or catalogue.next_audit_at is None
                or now < catalogue.last_verified_at
                or now >= catalogue.next_audit_at
            )
            intents: dict[tuple[SourceId, str], MatchEvaluationIntent] = {}
            for edition in editions:
                match = self.store.get_match_state(source.id, edition.source_native_id)
                fingerprint = matching_input_fingerprint(source, edition)
                prior_input = self.planner_journal.get_match_input(
                    str(source.id), edition.source_native_id
                )
                if match is None:
                    intents[edition.identity] = MatchEvaluationIntent.SEMANTIC_REEVALUATION
                    continue
                if prior_input is None:
                    intents[edition.identity] = (
                        MatchEvaluationIntent.EVIDENCE_RECOVERY
                        if match.status is MatchStatus.MATCHED
                        else MatchEvaluationIntent.SEMANTIC_REEVALUATION
                    )
                    continue
                if prior_input.input_fingerprint != fingerprint:
                    intents[edition.identity] = MatchEvaluationIntent.SEMANTIC_REEVALUATION
                    continue
                if match.status is MatchStatus.MATCHED:
                    if (
                        match.spotify_episode_uri is None
                        or not is_spotify_episode_uri(match.spotify_episode_uri)
                        or match.spotify_duration_seconds is None
                    ):
                        intents[edition.identity] = MatchEvaluationIntent.SEMANTIC_REEVALUATION
                    continue
                retry = self.planner_journal.get_unresolved(
                    str(source.id), edition.source_native_id
                )
                reason = unresolved_planning_reason(
                    retry,
                    input_fingerprint=fingerprint,
                    now=now,
                )
                if reason in {
                    PlannerReason.MISSING_EVIDENCE,
                    PlannerReason.INPUT_CHANGED,
                    PlannerReason.RETRY_DUE,
                }:
                    intents[edition.identity] = MatchEvaluationIntent.SEMANTIC_REEVALUATION
            return _SourceMatchingPlan(intents, audit_due)
        except Exception:
            return _SourceMatchingPlan(
                {edition.identity: MatchEvaluationIntent.EVIDENCE_RECOVERY for edition in editions},
                True,
                conservative=True,
            )

    def _build_source_work(
        self, now: datetime
    ) -> tuple[
        dict[SourceId, tuple[CanonicalEdition, ...]],
        dict[SourceId, _SourceMatchingPlan],
    ]:
        matching_cutoff = self._matching_cutoff(now)
        source_editions: dict[SourceId, tuple[CanonicalEdition, ...]] = {}
        source_plans: dict[SourceId, _SourceMatchingPlan] = {}
        for source in required_sources(self.config):
            editions = tuple(
                edition
                for edition in self.store.list_editions(source_id=source.id)
                if matching_cutoff <= edition.published_at <= now
            )
            source_editions[source.id] = editions
            source_plans[source.id] = self._source_matching_plan(source, editions, now)
        return source_editions, source_plans

    def _plan_destinations(
        self, now: datetime, unsafe_source_ids: set[SourceId]
    ) -> tuple[set[PlaylistId], set[PlaylistId]]:
        destination_work: set[PlaylistId] = set()
        persistence_blocked: set[PlaylistId] = set()
        for playlist in self.config.playlists:
            if not playlist.enabled or any(
                source_id in unsafe_source_ids for source_id in playlist.source_selection.explicit
            ):
                continue
            try:
                provisional = build_desired_state_from_store(
                    self.store,
                    playlist,
                    now=now,
                    source_timezones=self._source_timezones(),
                )
            except PersistenceError, DesiredStateError:
                self.destination_leases.revoke(playlist.id)
                persistence_blocked.add(playlist.id)
                continue
            expected = self._observe_desired_state(playlist, provisional.uris)
            if expected is None:
                persistence_blocked.add(playlist.id)
                continue
            plan = self._destination_work_plan(playlist, now, expected)
            if not plan.state_available:
                persistence_blocked.add(playlist.id)
            elif plan.needs_remote:
                destination_work.add(playlist.id)
        return destination_work, persistence_blocked

    def _authorization_context_matches(self, expected: str) -> bool:
        reader = self._planner_auth_reader
        if reader is None:
            return False
        try:
            current = reader()
            return isinstance(current, str) and current == expected
        except Exception:
            return False

    def _destination_verification_context(
        self, playlist: PlaylistDefinition, desired_uris: tuple[str, ...]
    ) -> DestinationVerificationContext | None:
        if self._shadow_auth_context is None:
            return None
        return DestinationVerificationContext(
            stable_fingerprint(desired_uris),
            destination_context_fingerprint(playlist),
            playlist_config_fingerprint(playlist),
            self._shadow_auth_context,
        )

    def _local_skip_still_authorized(
        self,
        playlist: PlaylistDefinition,
        expected: DestinationVerificationContext | None,
    ) -> bool:
        if expected is None or not self._authorization_context_matches(
            expected.auth_context_fingerprint
        ):
            self.destination_leases.revoke(playlist.id)
            return False
        guard = self.playlist_skip_guard
        try:
            config_matches = guard is None or guard(playlist)
        except Exception:
            config_matches = False
        if not config_matches:
            self.destination_leases.revoke(playlist.id)
            return False
        return True

    @contextmanager
    def _provider_mutation_guard(
        self, playlist: PlaylistDefinition, authorization_context: str
    ) -> Iterator[None]:
        if not self._authorization_context_matches(authorization_context):
            raise SpotifyReconciliationError(
                "Spotify authorization changed during cycle; destination preserved"
            )
        if self.playlist_mutation_guard is None:
            yield
            return
        with self.playlist_mutation_guard(playlist):
            if not self._authorization_context_matches(authorization_context):
                raise SpotifyReconciliationError(
                    "Spotify authorization changed during cycle; destination preserved"
                )
            yield

    def _observe_catalogue_inspection(
        self, source: SourceDefinition, observed_at: datetime
    ) -> None:
        try:
            if self._shadow_auth_context is None:
                return
            contract = catalogue_context_fingerprint(source, self._shadow_auth_context)
            self.planner_journal.verify_catalogue(contract, contract, verified_at=observed_at)
        except Exception:
            self._planner_shadow = PlannerShadowResult.unsafe()

    def _prepare_playlist_write(
        self,
        playlist: PlaylistDefinition,
        catalogue_coordinator: _CycleCatalogueCoordinator,
        observed_at: datetime,
        desired: DesiredPlaylistState,
    ) -> DesiredPlaylistState:
        """Validate every mapping selected for a possible mutation, then rebuild desired."""
        sources = {source.id: source for source in required_sources(self.config)}
        catalogues: dict[SourceId, tuple[SpotifyCatalogueResult, int]] = {}
        try:
            for item in desired.items:
                source = sources.get(item.source_id)
                if source is None:
                    raise SpotifyReconciliationError(
                        "Spotify prewrite validation could not resolve a configured source"
                    )
                current = self.store.get_match_state(item.source_id, item.source_native_id)
                if (
                    current is None
                    or current.status is not MatchStatus.MATCHED
                    or current.spotify_episode_uri != item.spotify_episode_uri
                ):
                    return build_desired_state_from_store(
                        self.store,
                        playlist,
                        now=observed_at,
                        source_timezones=self._source_timezones(),
                    )

                cached_catalogue = catalogues.get(source.id)
                if cached_catalogue is None:
                    cached_catalogue = catalogue_coordinator.get(source)
                    catalogues[source.id] = cached_catalogue
                    self._observe_catalogue_inspection(source, observed_at)
                catalogue, request_count = cached_catalogue
                if item.spotify_episode_uri in catalogue.candidate_uris:
                    continue
                if not catalogue.can_prove_absence:
                    raise SpotifyReconciliationError(
                        "Spotify prewrite validation could not establish mapping presence; "
                        "destination preserved"
                    )

                edition = self.store.get_edition(item.source_id, item.source_native_id)
                if edition is None:
                    raise SpotifyReconciliationError(
                        "Spotify prewrite validation lost its canonical edition; "
                        "destination preserved"
                    )
                result = match_source_editions(
                    catalogue_coordinator.client,
                    self.store,
                    source,
                    (edition,),
                    now=observed_at,
                    catalogue=catalogue,
                    evaluation_intents={
                        edition.identity: MatchEvaluationIntent.SEMANTIC_REEVALUATION
                    },
                    validate_cached=True,
                    catalogue_request_count=request_count,
                )
                self._observe_matching_result(source, result, observed_at)

            return build_desired_state_from_store(
                self.store,
                playlist,
                now=observed_at,
                source_timezones=self._source_timezones(),
            )
        except (PersistenceError, MatchConfigurationError, MatchResponseError) as exc:
            raise SpotifyReconciliationError(
                "Spotify prewrite mapping validation failed; destination preserved"
            ) from exc

    def _observe_matching_result(
        self,
        source: SourceDefinition,
        result: SourceMatchResult,
        observed_at: datetime,
    ) -> None:
        try:
            if self._shadow_auth_context is None:
                return
            contract = catalogue_context_fingerprint(source, self._shadow_auth_context)
            if result.catalogue_inspected:
                self.planner_journal.verify_catalogue(contract, contract, verified_at=observed_at)
            for outcome in result.outcomes:
                edition = outcome.edition
                input_fingerprint = matching_input_fingerprint(source, edition)
                if outcome.planner_input_evidence_qualified is True:
                    self.planner_journal.observe_match_input(
                        source_id=str(edition.source_id),
                        source_native_id=edition.source_native_id,
                        input_fingerprint=input_fingerprint,
                        observed_at=observed_at,
                    )
                elif outcome.planner_input_evidence_qualified is False:
                    self.planner_journal.clear_match_input(
                        str(edition.source_id), edition.source_native_id
                    )
                if outcome.status is MatchStatus.MATCHED:
                    self.planner_journal.clear_unresolved(
                        str(edition.source_id), edition.source_native_id
                    )
                elif not outcome.from_cache:
                    self.planner_journal.record_unresolved(
                        source_id=str(edition.source_id),
                        source_native_id=edition.source_native_id,
                        input_fingerprint=input_fingerprint,
                        status=outcome.status,
                        attempted_at=observed_at,
                    )
        except Exception:
            # Shadow evidence is fail-closed on its next read and cannot alter provider behavior.
            self._planner_shadow = PlannerShadowResult.unsafe()

    def _observe_destination_result(
        self,
        playlist: PlaylistDefinition,
        *,
        verified: bool,
        unknown: bool,
        observed_at: datetime,
        expected: DestinationVerificationContext | None = None,
    ) -> bool:
        try:
            if unknown:
                self.destination_leases.revoke(playlist.id)
                self.planner_journal.mark_destination_unknown(str(playlist.id))
            elif (
                verified
                and expected is not None
                and PendingSnapshotJournal(self.store).get(playlist.id) is None
            ):
                self.planner_journal.verify_destination(
                    str(playlist.id), expected=expected, verified_at=observed_at
                )
                return True
        except Exception:
            self.destination_leases.revoke(playlist.id)
            self._planner_shadow = PlannerShadowResult.unsafe()
        return False

    def _begin_destination_verification(self, playlist_id: PlaylistId) -> None:
        try:
            self.planner_journal.begin_destination_verification(str(playlist_id))
        except Exception:
            self._planner_shadow = PlannerShadowResult.unsafe()

    def _observe_desired_state(
        self, playlist: PlaylistDefinition, desired_uris: tuple[str, ...]
    ) -> DestinationVerificationContext | None:
        try:
            expected = self._destination_verification_context(playlist, desired_uris)
            if expected is None:
                return None
            self.destination_leases.revoke_unless(playlist.id, expected)
            self.planner_journal.observe_destination(
                playlist_id=str(playlist.id),
                destination_context_fingerprint=expected.destination_context_fingerprint,
                config_fingerprint=expected.config_fingerprint,
                auth_context_fingerprint=expected.auth_context_fingerprint,
                desired_fingerprint=expected.desired_fingerprint,
            )
            return expected
        except Exception:
            self.destination_leases.revoke(playlist.id)
            self._planner_shadow = PlannerShadowResult.unsafe()
            return None

    def _prune_operational_history(self, now: datetime) -> None:
        self.store.prune_operational_history(
            now=now,
            protected_identities=self._protected_identities(now),
        )

    def _source_timezones(self) -> Mapping[SourceId, ZoneInfo]:
        return {source.id: source.timezone for source in self.config.sources}

    def _request_diagnostics(
        self, playlist_id: PlaylistId, uris: tuple[str, ...]
    ) -> dict[str, Any]:
        encoded = json.dumps({"uris": list(uris)}).encode("utf-8")
        fingerprint = hashlib.sha256(
            json.dumps(uris, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        previous = self._previous_desired_fingerprints.get(playlist_id)
        self._previous_desired_fingerprints[playlist_id] = fingerprint
        valid = sum(is_spotify_episode_uri(uri) for uri in uris)
        return {
            "request_item_count": len(uris),
            "request_payload_bytes": len(encoded),
            "valid_uri_count": valid,
            "invalid_uri_count": len(uris) - valid,
            "duplicate_count": len(uris) - len(set(uris)),
            "desired_fingerprint": fingerprint,
            "desired_changed_since_previous_attempt": (
                None if previous is None else previous != fingerprint
            ),
        }

    def _record_failure(
        self, playlist_id: PlaylistId, occurred_at: datetime
    ) -> tuple[int, datetime]:
        prior = self._playlist_failure_streaks.get(playlist_id)
        result = (1, occurred_at) if prior is None else (prior[0] + 1, prior[1])
        self._playlist_failure_streaks[playlist_id] = result
        return result

    def _matching_cutoff(self, now: datetime) -> datetime:
        enabled = [
            playlist.retention_hours for playlist in self.config.playlists if playlist.enabled
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


def _spotify_client(access_token: str, *, metrics: SpotifyRequestMetrics) -> SpotifyEngineClient:
    return SpotifyClient(access_token=access_token, metrics=metrics)


def _combine_errors(first: str | None, second: str) -> str:
    return second if first is None else f"{first}; {second}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("engine timestamps must be timezone-aware")
    return value.astimezone(UTC)
