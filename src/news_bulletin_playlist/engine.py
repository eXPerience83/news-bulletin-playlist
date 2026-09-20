"""Authoritative Spotify-first engine.

The public Spotify surface is only a change detector. Official Spotify Web API show catalogues
are the episode authority, and persisted Spotify episode IDs/URIs are the runtime identity.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Protocol

from news_bulletin_playlist.desired_state import DesiredPlaylistState, DurationEligibilityDecision
from news_bulletin_playlist.models import (
    EngineConfig,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.reconciliation import (
    MutationOutcome,
    PlaylistReconciliationResult,
    SpotifyPlaylistClient,
    SpotifyReconciliationError,
    reconcile_spotify_playlist,
)
from news_bulletin_playlist.snapshot_pending import PendingSnapshotJournal
from news_bulletin_playlist.spotify.auth import SpotifyAuthError
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)
from news_bulletin_playlist.spotify.first_catalogue import (
    DEFAULT_MAX_PAGES,
    CatalogueResponseError,
    SpotifyCatalogueClient,
    fetch_source_catalogue,
)
from news_bulletin_playlist.spotify.first_desired import (
    SpotifyFirstDesiredBuild,
    build_spotify_first_desired_state,
    desired_fingerprint,
)
from news_bulletin_playlist.spotify.first_models import RefreshReason, as_utc
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetectorCycleSummary,
    PublicDetectorJournal,
    PublicDetector,
    PublicDetectorState,
    spotify_show_id,
)
from news_bulletin_playlist.spotify.uris import is_spotify_episode_uri
from news_bulletin_playlist.spotify_backoff import (
    ProviderAuthorizationSession,
    SpotifyBackoffAuthProvider,
    SpotifyRateLimitBackoffActive,
    SpotifyRateLimitGuardAuth,
    SpotifyRateLimitGuardClient,
    SpotifyRateLimitJournal,
    SpotifyRateLimitStateUnavailable,
    SpotifyRateLimitSuppressed,
)
from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetrics

DEFAULT_ENGINE_INTERVAL = timedelta(minutes=10)
DEFAULT_SOURCE_INACTIVITY = timedelta(hours=24)
DEFAULT_DESTINATION_AUDIT = timedelta(hours=6)
SPOTIFY_FIRST_STATE_RETENTION = timedelta(days=30)


class SpotifyAuthProvider(SpotifyBackoffAuthProvider, Protocol):
    def acquire_provider_session(
        self,
        *,
        now: datetime | None = None,
    ) -> ProviderAuthorizationSession: ...

    def planner_authorization_context(self) -> str | None: ...


class SpotifyEngineClient(
    SpotifyCatalogueClient,
    SpotifyPlaylistClient,
    Protocol,
):
    pass


class EngineCycleRunner(Protocol):
    def run_cycle(self) -> EngineCycleResult: ...


SpotifyClientFactory = Callable[[str], SpotifyEngineClient]
Clock = Callable[[], datetime]
PlaylistMutationGuard = Callable[
    [PlaylistDefinition],
    AbstractContextManager[None],
]


class EngineCycleAlreadyRunning(RuntimeError):
    """Raised when a second engine cycle overlaps the current cycle."""


@dataclass(frozen=True, slots=True)
class SourceCycleOutcome:
    source_id: SourceId
    ok: bool
    episode_count: int
    refreshed: bool
    refresh_reason: str | None
    api_request_count: int
    last_success_at: datetime | None
    detector_ok: bool
    detector_changed: bool
    detector_false_negative: bool = False
    parser_fallback_count: int = 0
    filter_rejected_count: int = 0
    removed_count: int = 0
    error: str | None = None


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
    duration_unavailable_count: int = 0
    request_item_count: int | None = None
    request_payload_bytes: int | None = None
    valid_uri_count: int | None = None
    invalid_uri_count: int | None = None
    duplicate_count: int | None = None
    desired_fingerprint: str | None = None
    local_skip: bool = False
    stale_escalation: bool = False
    write_outcome: str | None = None


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
    """Thread-safe status shared by the scheduler and read-only HTTP surfaces."""

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
    """Execute one complete Spotify-first source/destination cycle."""

    def __init__(
        self,
        config: EngineConfig,
        store: SQLiteStore,
        auth: SpotifyAuthProvider,
        public_detector: PublicDetector,
        detector_journal: PublicDetectorJournal,
        first_journal: SpotifyFirstJournal,
        *,
        client_factory: SpotifyClientFactory | None = None,
        clock: Clock | None = None,
        playlist_mutation_guard: PlaylistMutationGuard | None = None,
        request_metrics: SpotifyRequestMetrics | None = None,
        source_inactivity: timedelta = DEFAULT_SOURCE_INACTIVITY,
        destination_audit: timedelta = DEFAULT_DESTINATION_AUDIT,
        max_catalogue_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        if source_inactivity <= timedelta(0):
            raise ValueError("source inactivity threshold must be positive")
        if destination_audit <= timedelta(0):
            raise ValueError("destination audit threshold must be positive")
        if max_catalogue_pages <= 0:
            raise ValueError("max catalogue pages must be positive")

        self.config = config
        self.store = store
        self.raw_auth = auth
        self.public_detector = public_detector
        self.detector_journal = detector_journal
        self.first_journal = first_journal
        self.clock = clock or _utc_now
        self.playlist_mutation_guard = playlist_mutation_guard
        self.source_inactivity = source_inactivity
        self.destination_audit = destination_audit
        self.max_catalogue_pages = max_catalogue_pages
        self.rate_limit_journal = SpotifyRateLimitJournal(store)
        self.auth = SpotifyRateLimitGuardAuth(
            auth,
            self.rate_limit_journal,
            clock=self.clock,
        )
        self.request_metrics = request_metrics or SpotifyRequestMetrics(store.path)
        self.request_metrics.initialize()

        base_factory = client_factory or partial(
            _spotify_client,
            metrics=self.request_metrics,
        )

        def guarded_factory(access_token: str) -> SpotifyEngineClient:
            return SpotifyRateLimitGuardClient(
                base_factory(access_token),
                self.rate_limit_journal,
                clock=self.clock,
            )

        self.client_factory = guarded_factory
        self._cycle_lock = threading.Lock()

    def run_cycle(self) -> EngineCycleResult:
        if not self._cycle_lock.acquire(blocking=False):
            raise EngineCycleAlreadyRunning("engine cycle is already running")
        try:
            return self._run_cycle()
        finally:
            self._cycle_lock.release()

    def _run_cycle(self) -> EngineCycleResult:
        started_at = as_utc(self.clock())
        required = _required_sources(self.config)
        detector = self.public_detector.observe_sources(
            required,
            observed_at=started_at,
        )
        refresh_plan = self._refresh_plan(
            detector,
            now=started_at,
        )

        session: ProviderAuthorizationSession | None = None
        client: SpotifyEngineClient | None = None
        source_outcomes: dict[SourceId, SourceCycleOutcome] = {}

        if refresh_plan:
            try:
                session = self.auth.acquire_provider_session(now=started_at)
                client = self.client_factory(session.access_token)
            except (
                SpotifyAuthError,
                PersistenceError,
                SpotifyRateLimitBackoffActive,
                SpotifyRateLimitStateUnavailable,
            ) as exc:
                source_outcomes.update(
                    self._source_authorization_failure(
                        required,
                        refresh_plan,
                        detector,
                        exc,
                    )
                )
            else:
                for source_id, reason in refresh_plan:
                    source = next(source for source in required if source.id == source_id)
                    source_outcomes[source.id] = self._refresh_source(
                        source,
                        reason,
                        detector,
                        client,
                        now=started_at,
                    )

        for source in required:
            if source.id not in source_outcomes:
                source_outcomes[source.id] = self._unchanged_source_outcome(
                    source.id,
                    detector,
                    now=started_at,
                )

        playlist_outcomes: list[PlaylistCycleOutcome] = []
        episodes = self.first_journal.list_episodes()
        sources = {source.id: source for source in self.config.sources}

        for playlist in self.config.playlists:
            if not playlist.enabled:
                continue

            if not self._playlist_sources_ready(playlist, now=started_at):
                playlist_outcomes.append(
                    self._preserved_playlist_outcome(
                        playlist,
                        "destination preserved because Spotify-first source state is incomplete",
                        now=started_at,
                    )
                )
                continue

            build = build_spotify_first_desired_state(
                playlist,
                episodes,
                sources=sources,
                now=started_at,
            )
            desired = build.desired
            fingerprint = desired_fingerprint(desired.uris)

            if self._can_skip_destination(
                playlist,
                fingerprint,
                now=started_at,
            ):
                self.store.record_playlist_run(
                    playlist.id,
                    started_at=started_at,
                    finished_at=started_at,
                    ok=True,
                    desired_count=len(desired.items),
                    applied_count=len(desired.items),
                )
                state = self.store.get_playlist_state(playlist.id)
                playlist_outcomes.append(
                    self._playlist_outcome(
                        playlist,
                        build,
                        ok=True,
                        applied_count=len(desired.items),
                        wrote=False,
                        last_success_at=None if state is None else state.last_success_at,
                        fingerprint=fingerprint,
                        local_skip=True,
                        write_outcome="skipped",
                    )
                )
                continue

            if session is None or client is None:
                try:
                    session = self.auth.acquire_provider_session(now=started_at)
                    client = self.client_factory(session.access_token)
                except (
                    SpotifyAuthError,
                    PersistenceError,
                    SpotifyRateLimitBackoffActive,
                    SpotifyRateLimitStateUnavailable,
                ) as exc:
                    playlist_outcomes.append(
                        self._preserved_playlist_outcome(
                            playlist,
                            _safe_error(exc),
                            build=build,
                            fingerprint=fingerprint,
                            now=started_at,
                        )
                    )
                    continue

            outcome = self._reconcile_playlist(
                playlist,
                build,
                fingerprint,
                client,
                session,
                now=started_at,
            )
            playlist_outcomes.append(outcome)
            episodes = self.first_journal.list_episodes()

        with suppress(PersistenceError):
            self.first_journal.prune(before=started_at - SPOTIFY_FIRST_STATE_RETENTION)

        ordered_sources = tuple(source_outcomes[source.id] for source in required)
        ok = all(source.ok for source in ordered_sources) and all(
            playlist.ok for playlist in playlist_outcomes
        )
        finished_at = as_utc(self.clock())
        return EngineCycleResult(
            started_at=started_at,
            finished_at=finished_at,
            ok=ok,
            sources=ordered_sources,
            playlists=tuple(playlist_outcomes),
            error=None if ok else "cycle completed with preserved or degraded work",
        )

    def _refresh_plan(
        self,
        detector: PublicDetectorCycleSummary,
        *,
        now: datetime,
    ) -> tuple[tuple[SourceId, RefreshReason], ...]:
        changed = set(detector.changed_source_ids)
        plan: list[tuple[SourceId, RefreshReason]] = []
        for source in _required_sources(self.config):
            local = self.first_journal.get_source_state(source.id)
            public = self.detector_journal.get(source.id, spotify_show_id(source))
            if local is None or local.spotify_show_id != spotify_show_id(source):
                plan.append((source.id, RefreshReason.BOOTSTRAP))
                continue
            if source.id in changed:
                plan.append((source.id, RefreshReason.CHANGE))
                continue
            if (
                public is not None
                and public.last_change_at is not None
                and public.last_change_at > local.last_refresh_at
            ):
                plan.append((source.id, RefreshReason.CHANGE))
                continue
            due = (
                public.audit_due(now=now, inactivity=self.source_inactivity)
                if public is not None
                else now - local.last_refresh_at >= self.source_inactivity
            )
            if due:
                plan.append((source.id, RefreshReason.INACTIVITY_AUDIT))
        return tuple(plan)

    def _refresh_source(
        self,
        source: SourceDefinition,
        reason: RefreshReason,
        detector: PublicDetectorCycleSummary,
        client: SpotifyEngineClient,
        *,
        now: datetime,
    ) -> SourceCycleOutcome:
        cutoff = now - timedelta(hours=_max_retention(self.config, source.id))
        public = self.detector_journal.get(source.id, spotify_show_id(source))
        try:
            snapshot = fetch_source_catalogue(
                client,
                source,
                now=now,
                cutoff=cutoff,
                max_pages=self.max_catalogue_pages,
            )
            counts = self.first_journal.replace_source_episodes(
                source.id,
                snapshot.episodes,
                coverage_proven=snapshot.coverage_proven,
            )
            self.first_journal.record_refresh(
                source,
                refreshed_at=now,
                reason=reason,
                detector_episode_id=None if public is None else public.last_episode_id,
                api_episode_id=snapshot.api_latest_id,
                request_count=snapshot.request_count,
                complete=snapshot.complete,
                coverage_proven=snapshot.coverage_proven,
                coverage_cutoff_at=cutoff,
            )
            self.detector_journal.mark_catalogue_refresh(
                source.id,
                spotify_show_id(source),
                refreshed_at=now,
            )
            false_negative = _possible_false_negative(
                reason,
                None if public is None else public.last_episode_id,
                snapshot.api_latest_id,
            )
            return SourceCycleOutcome(
                source_id=source.id,
                ok=snapshot.coverage_proven,
                episode_count=len(snapshot.episodes),
                refreshed=True,
                refresh_reason=reason.value,
                api_request_count=snapshot.request_count,
                last_success_at=now,
                detector_ok=_detector_ok(public, now),
                detector_changed=source.id in detector.changed_source_ids,
                detector_false_negative=false_negative,
                parser_fallback_count=snapshot.parser_fallback_count,
                filter_rejected_count=snapshot.filter_rejected_count,
                removed_count=counts.removed,
                error=(
                    None
                    if snapshot.coverage_proven
                    else "official Spotify catalogue did not prove retention-window coverage"
                ),
            )
        except (
            CatalogueResponseError,
            SpotifyApiError,
            SpotifyTransportError,
            SpotifyRateLimitSuppressed,
            PersistenceError,
            ValueError,
        ) as exc:
            state = self.first_journal.get_source_state(source.id)
            return SourceCycleOutcome(
                source_id=source.id,
                ok=state is not None and state.coverage_proven,
                episode_count=len(self.first_journal.list_episodes(source_ids=(source.id,))),
                refreshed=False,
                refresh_reason=reason.value,
                api_request_count=0,
                last_success_at=None if state is None else state.last_refresh_at,
                detector_ok=_detector_ok(public, now),
                detector_changed=source.id in detector.changed_source_ids,
                error=_safe_error(exc),
            )

    def _unchanged_source_outcome(
        self,
        source_id: SourceId,
        detector: PublicDetectorCycleSummary,
        *,
        now: datetime,
    ) -> SourceCycleOutcome:
        source = next(source for source in _required_sources(self.config) if source.id == source_id)
        public = self.detector_journal.get(source.id, spotify_show_id(source))
        state = self.first_journal.get_source_state(source_id)
        episodes = self.first_journal.list_episodes(source_ids=(source_id,))
        return SourceCycleOutcome(
            source_id=source_id,
            ok=state is not None and state.coverage_proven,
            episode_count=len(episodes),
            refreshed=False,
            refresh_reason=None,
            api_request_count=0,
            last_success_at=None if state is None else state.last_refresh_at,
            detector_ok=_detector_ok(public, now),
            detector_changed=source_id in detector.changed_source_ids,
            error=None if state is not None else "Spotify-first source has no persisted catalogue",
        )

    def _source_authorization_failure(
        self,
        required: tuple[SourceDefinition, ...],
        plan: tuple[tuple[SourceId, RefreshReason], ...],
        detector: PublicDetectorCycleSummary,
        exc: Exception,
    ) -> dict[SourceId, SourceCycleOutcome]:
        planned = dict(plan)
        outcomes: dict[SourceId, SourceCycleOutcome] = {}
        now = as_utc(self.clock())
        for source in required:
            reason = planned.get(source.id)
            if reason is None:
                continue
            state = self.first_journal.get_source_state(source.id)
            public = self.detector_journal.get(source.id, spotify_show_id(source))
            outcomes[source.id] = SourceCycleOutcome(
                source_id=source.id,
                ok=state is not None and state.coverage_proven,
                episode_count=len(self.first_journal.list_episodes(source_ids=(source.id,))),
                refreshed=False,
                refresh_reason=reason.value,
                api_request_count=0,
                last_success_at=None if state is None else state.last_refresh_at,
                detector_ok=_detector_ok(public, now),
                detector_changed=source.id in detector.changed_source_ids,
                error=_safe_error(exc),
            )
        return outcomes

    def _playlist_sources_ready(
        self,
        playlist: PlaylistDefinition,
        *,
        now: datetime,
    ) -> bool:
        cutoff = now - timedelta(hours=playlist.retention_hours)
        sources = {source.id: source for source in self.config.sources}
        for source_id in playlist.source_selection.explicit:
            source = sources.get(source_id)
            if source is None:
                return False
            state = self.first_journal.get_source_state(source_id)
            if (
                state is None
                or state.spotify_show_id != spotify_show_id(source)
                or not state.coverage_proven
                or state.coverage_cutoff_at > cutoff
            ):
                return False
        return True

    def _can_skip_destination(
        self,
        playlist: PlaylistDefinition,
        fingerprint: str,
        *,
        now: datetime,
    ) -> bool:
        if PendingSnapshotJournal(self.store).get(playlist.id) is not None:
            return False
        state = self.first_journal.get_destination_verification(playlist.id)
        return bool(
            state is not None
            and state.destination_id == playlist.destination.external_id
            and state.desired_fingerprint == fingerprint
            and state.verified_at <= now
            and now - state.verified_at < self.destination_audit
        )

    def _reconcile_playlist(
        self,
        playlist: PlaylistDefinition,
        build: SpotifyFirstDesiredBuild,
        fingerprint: str,
        client: SpotifyEngineClient,
        session: ProviderAuthorizationSession,
        *,
        now: datetime,
    ) -> PlaylistCycleOutcome:
        desired = build.desired
        stale_escalation = False
        try:
            result = reconcile_spotify_playlist(
                client,
                playlist,
                desired,
                store=self.store,
                mutation_guard=partial(
                    self._provider_mutation_guard,
                    playlist,
                    session.authorization_context,
                ),
                prepare_write=lambda _current: self._rebuild_desired(playlist, now=now),
                snapshot_attested_prewrite=True,
            )
        except SpotifyReconciliationError as exc:
            retry = self._stale_rejection_retry(
                playlist,
                desired,
                client,
                session,
                exc,
                now=now,
            )
            if retry is None:
                return self._record_playlist_failure(
                    playlist,
                    build,
                    fingerprint,
                    exc,
                    now=now,
                )
            result, build = retry
            desired = build.desired
            fingerprint = desired_fingerprint(desired.uris)
            stale_escalation = True
        except (
            SpotifyApiError,
            SpotifyTransportError,
            SpotifyRateLimitSuppressed,
            PersistenceError,
        ) as exc:
            return self._record_playlist_failure(
                playlist,
                build,
                fingerprint,
                exc,
                now=now,
            )

        if result.remote_evidence_qualified:
            self.first_journal.record_destination_verification(
                playlist.id,
                destination_id=playlist.destination.external_id,
                desired_fingerprint=fingerprint,
                verified_at=now,
            )
        else:
            self.first_journal.clear_destination_verification(playlist.id)

        self.store.record_playlist_run(
            playlist.id,
            started_at=now,
            finished_at=now,
            ok=result.ok,
            desired_count=result.desired_count,
            applied_count=0 if result.applied_count is None else result.applied_count,
            error=result.error or result.warning,
        )
        state = self.store.get_playlist_state(playlist.id)
        return self._playlist_outcome(
            playlist,
            build,
            ok=result.ok,
            applied_count=result.applied_count,
            wrote=result.wrote,
            last_success_at=None if state is None else state.last_success_at,
            fingerprint=fingerprint,
            stale_escalation=stale_escalation,
            error=result.error or result.warning,
            write_outcome=result.mutation_outcome.value,
        )

    def _stale_rejection_retry(
        self,
        playlist: PlaylistDefinition,
        old_desired: DesiredPlaylistState,
        client: SpotifyEngineClient,
        session: ProviderAuthorizationSession,
        exc: SpotifyReconciliationError,
        *,
        now: datetime,
    ) -> tuple[PlaylistReconciliationResult, SpotifyFirstDesiredBuild] | None:
        if _caused_by_http_status(exc) != 400:
            return None

        selected = set(playlist.source_selection.explicit)
        sources = {
            source.id: source for source in _required_sources(self.config) if source.id in selected
        }
        cutoff = now - timedelta(hours=playlist.retention_hours)

        try:
            for source in sources.values():
                snapshot = fetch_source_catalogue(
                    client,
                    source,
                    now=now,
                    cutoff=cutoff,
                    max_pages=self.max_catalogue_pages,
                )
                if not snapshot.coverage_proven:
                    return None
                self.first_journal.replace_source_episodes(
                    source.id,
                    snapshot.episodes,
                    coverage_proven=True,
                )
                public = self.detector_journal.get(source.id, spotify_show_id(source))
                self.first_journal.record_refresh(
                    source,
                    refreshed_at=now,
                    reason=RefreshReason.STALE_ESCALATION,
                    detector_episode_id=None if public is None else public.last_episode_id,
                    api_episode_id=snapshot.api_latest_id,
                    request_count=snapshot.request_count,
                    complete=snapshot.complete,
                    coverage_proven=True,
                    coverage_cutoff_at=cutoff,
                )
                self.detector_journal.mark_catalogue_refresh(
                    source.id,
                    spotify_show_id(source),
                    refreshed_at=now,
                )

            build = self._rebuild_build(playlist, now=now)
            removed = set(old_desired.uris) - set(build.desired.uris)
            if not removed:
                return None

            result = reconcile_spotify_playlist(
                client,
                playlist,
                build.desired,
                store=self.store,
                mutation_guard=partial(
                    self._provider_mutation_guard,
                    playlist,
                    session.authorization_context,
                ),
                prepare_write=lambda _current: self._rebuild_desired(playlist, now=now),
                snapshot_attested_prewrite=False,
            )
            return result, build
        except (
            CatalogueResponseError,
            SpotifyApiError,
            SpotifyTransportError,
            SpotifyRateLimitSuppressed,
            PersistenceError,
            SpotifyReconciliationError,
            ValueError,
        ):
            return None

    def _rebuild_build(
        self,
        playlist: PlaylistDefinition,
        *,
        now: datetime,
    ) -> SpotifyFirstDesiredBuild:
        return build_spotify_first_desired_state(
            playlist,
            self.first_journal.list_episodes(),
            sources={source.id: source for source in self.config.sources},
            now=now,
        )

    def _rebuild_desired(
        self,
        playlist: PlaylistDefinition,
        *,
        now: datetime,
    ) -> DesiredPlaylistState:
        return self._rebuild_build(playlist, now=now).desired

    @contextmanager
    def _provider_mutation_guard(
        self,
        playlist: PlaylistDefinition,
        authorization_context: str,
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

    def _authorization_context_matches(self, expected: str) -> bool:
        try:
            current = self.raw_auth.planner_authorization_context()
        except Exception:
            return False
        return isinstance(current, str) and current == expected

    def _record_playlist_failure(
        self,
        playlist: PlaylistDefinition,
        build: SpotifyFirstDesiredBuild,
        fingerprint: str,
        exc: Exception,
        *,
        now: datetime,
    ) -> PlaylistCycleOutcome:
        self.first_journal.clear_destination_verification(playlist.id)
        self.store.record_playlist_run(
            playlist.id,
            started_at=now,
            finished_at=now,
            ok=False,
            desired_count=len(build.desired.items),
            applied_count=0,
            error=_safe_error(exc),
        )
        state = self.store.get_playlist_state(playlist.id)
        return self._playlist_outcome(
            playlist,
            build,
            ok=False,
            applied_count=None,
            wrote=None,
            last_success_at=None if state is None else state.last_success_at,
            fingerprint=fingerprint,
            error=_safe_error(exc),
            write_outcome=MutationOutcome.NOT_ATTEMPTED.value,
        )

    def _preserved_playlist_outcome(
        self,
        playlist: PlaylistDefinition,
        error: str,
        *,
        build: SpotifyFirstDesiredBuild | None = None,
        fingerprint: str | None = None,
        now: datetime,
    ) -> PlaylistCycleOutcome:
        desired_count = 0 if build is None else len(build.desired.items)
        self.store.record_playlist_run(
            playlist.id,
            started_at=now,
            finished_at=now,
            ok=False,
            desired_count=desired_count,
            applied_count=0,
            error=error,
        )
        state = self.store.get_playlist_state(playlist.id)
        if build is None:
            return PlaylistCycleOutcome(
                playlist_id=playlist.id,
                ok=False,
                desired_count=0,
                applied_count=None,
                wrote=None,
                last_success_at=None if state is None else state.last_success_at,
                error=error,
                desired_fingerprint=fingerprint,
                write_outcome=MutationOutcome.NOT_ATTEMPTED.value,
            )
        return self._playlist_outcome(
            playlist,
            build,
            ok=False,
            applied_count=None,
            wrote=None,
            last_success_at=None if state is None else state.last_success_at,
            fingerprint=fingerprint or desired_fingerprint(build.desired.uris),
            error=error,
            write_outcome=MutationOutcome.NOT_ATTEMPTED.value,
        )

    def _playlist_outcome(
        self,
        playlist: PlaylistDefinition,
        build: SpotifyFirstDesiredBuild,
        *,
        ok: bool,
        applied_count: int | None,
        wrote: bool | None,
        last_success_at: datetime | None,
        fingerprint: str,
        local_skip: bool = False,
        stale_escalation: bool = False,
        error: str | None = None,
        write_outcome: str | None = None,
    ) -> PlaylistCycleOutcome:
        uris = build.desired.uris
        encoded = json.dumps({"uris": list(uris)}, separators=(",", ":")).encode("utf-8")
        valid = sum(is_spotify_episode_uri(uri) for uri in uris)
        return PlaylistCycleOutcome(
            playlist_id=playlist.id,
            ok=ok,
            desired_count=len(uris),
            applied_count=applied_count,
            wrote=wrote,
            last_success_at=last_success_at,
            error=error,
            duration_decisions=build.desired.duration_decisions,
            duration_unavailable_count=build.duration_unavailable_count,
            request_item_count=len(uris),
            request_payload_bytes=len(encoded),
            valid_uri_count=valid,
            invalid_uri_count=len(uris) - valid,
            duplicate_count=len(uris) - len(set(uris)),
            desired_fingerprint=fingerprint,
            local_skip=local_skip,
            stale_escalation=stale_escalation,
            write_outcome=write_outcome,
        )


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
        self._wake_event.set()

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.status.begin_cycle()
            try:
                result = self.runner.run_cycle()
            except Exception as exc:  # noqa: BLE001
                observed_at = as_utc(self.clock())
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

            self.status.finish_cycle(
                result,
                next_run_at=as_utc(self.clock()) + self.interval,
            )
            self._wait_for_wake_or_stop(stop_event)

        self.status.set_next_run(None)

    def _wait_for_wake_or_stop(self, stop_event: threading.Event) -> None:
        deadline = time.monotonic() + self.interval.total_seconds()
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._wake_event.wait(min(remaining, 0.25)):
                self._wake_event.clear()
                return


def _required_sources(config: EngineConfig) -> tuple[SourceDefinition, ...]:
    required_ids = {
        source_id
        for playlist in config.playlists
        if playlist.enabled
        for source_id in playlist.source_selection.explicit
    }
    return tuple(
        source for source in config.sources if source.enabled and source.id in required_ids
    )


def _max_retention(config: EngineConfig, source_id: SourceId) -> int:
    return max(
        (
            playlist.retention_hours
            for playlist in config.playlists
            if playlist.enabled and source_id in playlist.source_selection.explicit
        ),
        default=48,
    )


def _detector_ok(public: PublicDetectorState | None, observed_at: datetime) -> bool:
    if public is None:
        return False
    return bool(
        public.last_detector_success_at is not None
        and (
            public.last_failure_at is None
            or public.last_detector_success_at >= public.last_failure_at
        )
        and public.last_detector_success_at <= observed_at
    )


def _possible_false_negative(
    reason: RefreshReason,
    detector_episode_id: str | None,
    api_latest_id: str | None,
) -> bool:
    return bool(
        reason is RefreshReason.INACTIVITY_AUDIT
        and detector_episode_id is not None
        and api_latest_id is not None
        and detector_episode_id != api_latest_id
    )


def _caused_by_http_status(exc: BaseException) -> int | None:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, SpotifyApiError):
            return current.status
        current = current.__cause__
    return None


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, SpotifyRateLimitBackoffActive):
        return "Spotify rate-limit cooldown is active; destination preserved"
    if isinstance(exc, SpotifyRateLimitStateUnavailable):
        return "Spotify rate-limit state is unavailable; destination preserved"
    if isinstance(exc, SpotifyRateLimitSuppressed):
        return "Spotify request suppressed by global rate-limit cooldown"
    if isinstance(exc, SpotifyApiError):
        return f"Spotify API request failed (http_status={exc.status})"
    if isinstance(exc, SpotifyTransportError):
        return "Spotify request failed due to a transport error"
    if isinstance(exc, CatalogueResponseError):
        return "Spotify catalogue response was invalid"
    if isinstance(exc, PersistenceError):
        return "Spotify-first persistence is unavailable"
    if isinstance(exc, SpotifyReconciliationError):
        return str(exc)
    if isinstance(exc, SpotifyAuthError):
        return "Spotify authorization is unavailable"
    return type(exc).__name__


def _spotify_client(
    access_token: str,
    *,
    metrics: SpotifyRequestMetrics,
) -> SpotifyEngineClient:
    return SpotifyClient(access_token=access_token, metrics=metrics)


def _utc_now() -> datetime:
    return datetime.now(UTC)
