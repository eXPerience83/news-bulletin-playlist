from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
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
    DesiredStateError,
    DurationEligibilityDecision,
)
from news_bulletin_playlist.models import (
    EngineConfig,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.persistence import MatchStatus, PersistenceError, SQLiteStore
from news_bulletin_playlist.reconciliation import (
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
    MatchConfigurationError,
    MatchResponseError,
    SourceMatchResult,
    match_source_editions,
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
    PlannerReason,
    PlannerShadowResult,
    SpotifyPlannerJournal,
    destination_context_fingerprint,
    edition_input_fingerprint,
    plan_shadow_cycle,
    playlist_config_fingerprint,
    source_contract_fingerprint,
    stable_fingerprint,
    unresolved_planning_reason,
)

DEFAULT_ENGINE_INTERVAL = timedelta(minutes=10)


class SpotifyAuthProvider(SpotifyBackoffAuthProvider, Protocol):
    """Authentication surface required by the production engine."""


class SpotifyEngineClient(SpotifyBackoffClient, Protocol):
    """Combined Spotify catalogue/playlist surface required by the production engine."""


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
    ) -> None:
        self.config = config
        self.store = store
        self.fetcher = fetcher
        self.clock = clock or _utc_now
        self.rate_limit_journal = SpotifyRateLimitJournal(store)
        self.request_metrics = request_metrics or SpotifyRequestMetrics(store.path)
        self.request_metrics.initialize()
        self.planner_journal = SpotifyPlannerJournal(store)
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

        # Shadow-only: this result is never consulted to gate auth, matching, reads or writes.
        self._planner_shadow = self._compute_shadow_plan(collected_at)

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
                        outcome.spotify_episode_uri is not None for outcome in match_result.outcomes
                    )
                    self._observe_matching_result(source, match_result, matching_now)
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

            try:
                desired = build_desired_state_from_store(
                    self.store,
                    playlist,
                    now=playlist_started_at,
                    source_timezones=self._source_timezones(),
                )
                desired_count = len(desired.items)
                request_diagnostics = self._request_diagnostics(playlist.id, desired.uris)
                self._observe_desired_state(playlist, desired.uris)
                self._begin_destination_verification(playlist.id)
                mutation_guard = (
                    None
                    if self.playlist_mutation_guard is None
                    else partial(self.playlist_mutation_guard, playlist)
                )
                reconciled = reconcile_spotify_playlist(
                    client,
                    playlist,
                    desired,
                    store=self.store,
                    mutation_guard=mutation_guard,
                )
                playlist_finished_at = _as_utc(self.clock())
                self._observe_destination_result(
                    playlist,
                    verified=True,
                    unknown=False,
                    observed_at=playlist_finished_at,
                )
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
                    unknown="write_outcome=unknown" in error_message,
                    observed_at=playlist_finished_at,
                )
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
                            "unknown" if "write_outcome=unknown" in error_message else None
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
            planner_shadow=self._planner_shadow,
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
        try:
            matching_cutoff = self._matching_cutoff(now)
            source_reasons: list[tuple[str, PlannerReason]] = []
            additional_reasons: set[PlannerReason] = set()
            catalogue_states: list[CatalogueState | None] = []
            for source in required_sources(self.config):
                context = source_contract_fingerprint(source)
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
                    fingerprint = edition_input_fingerprint(edition)
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
            auth_context = stable_fingerprint(("spotify-auth-context-v1",))
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
                    source_contract_fingerprint(source) for source in required_sources(self.config)
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
        except PersistenceError, DesiredStateError, ValueError:
            return PlannerShadowResult.unsafe()

    def _observe_matching_result(
        self,
        source: SourceDefinition,
        result: SourceMatchResult,
        observed_at: datetime,
    ) -> None:
        try:
            contract = source_contract_fingerprint(source)
            if (
                result.catalogue_calls
                and result.catalogue_complete
                and result.catalogue_candidate_count
            ):
                self.planner_journal.verify_catalogue(contract, contract, verified_at=observed_at)
            for outcome in result.outcomes:
                edition = outcome.edition
                input_fingerprint = edition_input_fingerprint(edition)
                qualifying_catalogue = (
                    result.catalogue_calls > 0
                    and result.catalogue_complete
                    and result.catalogue_candidate_count > 0
                )
                if not outcome.from_cache or qualifying_catalogue:
                    self.planner_journal.observe_match_input(
                        source_id=str(edition.source_id),
                        source_native_id=edition.source_native_id,
                        input_fingerprint=input_fingerprint,
                        observed_at=observed_at,
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
        except PersistenceError, ValueError:
            # Shadow evidence is fail-closed on its next read and cannot alter provider behavior.
            self._planner_shadow = PlannerShadowResult.unsafe()

    def _observe_destination_result(
        self,
        playlist: PlaylistDefinition,
        *,
        verified: bool,
        unknown: bool,
        observed_at: datetime,
    ) -> None:
        try:
            if unknown:
                self.planner_journal.mark_destination_unknown(str(playlist.id))
            elif verified and PendingSnapshotJournal(self.store).get(playlist.id) is None:
                self.planner_journal.verify_destination(str(playlist.id), verified_at=observed_at)
        except PersistenceError, ValueError:
            self._planner_shadow = PlannerShadowResult.unsafe()

    def _begin_destination_verification(self, playlist_id: PlaylistId) -> None:
        try:
            self.planner_journal.begin_destination_verification(str(playlist_id))
        except PersistenceError, ValueError:
            self._planner_shadow = PlannerShadowResult.unsafe()

    def _observe_desired_state(
        self, playlist: PlaylistDefinition, desired_uris: tuple[str, ...]
    ) -> None:
        try:
            self.planner_journal.observe_destination(
                playlist_id=str(playlist.id),
                destination_context_fingerprint=destination_context_fingerprint(playlist),
                config_fingerprint=playlist_config_fingerprint(playlist),
                auth_context_fingerprint=stable_fingerprint(("spotify-auth-context-v1",)),
                desired_fingerprint=stable_fingerprint(desired_uris),
            )
        except PersistenceError, ValueError:
            self._planner_shadow = PlannerShadowResult.unsafe()

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
