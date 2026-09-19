"""Non-authoritative Spotify-first Phase C coordinator."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from news_bulletin_playlist.collection import required_sources
from news_bulletin_playlist.desired_state import DesiredStateError
from news_bulletin_playlist.models import EngineConfig, PlaylistId, SourceDefinition, SourceId
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify.auth import SpotifyAuthError
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)
from news_bulletin_playlist.spotify.first_catalogue import (
    DEFAULT_MAX_PAGES,
    fetch_source_catalogue,
)
from news_bulletin_playlist.spotify.first_desired import (
    build_legacy_desired_state,
    build_shadow_desired_state,
    compare_desired_states,
    comparison_is_valid,
)
from news_bulletin_playlist.spotify.first_diagnostics import (
    emit_detector_correlation,
    emit_failure,
    emit_parser_fallbacks,
    emit_playlist_comparison,
    emit_refresh,
)
from news_bulletin_playlist.spotify.first_models import RefreshReason, SourceShadowState, as_utc
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.matcher import (
    MatchConfigurationError,
    MatchResponseError,
    SpotifyCatalogClient,
)
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetectorCycleSummary,
    PublicDetectorJournal,
    PublicDetectorState,
    spotify_show_id,
)
from news_bulletin_playlist.spotify_backoff import (
    SpotifyBackoffAuthProvider,
    SpotifyRateLimitGuardAuth,
    SpotifyRateLimitGuardClient,
    SpotifyRateLimitJournal,
)
from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetrics

DEFAULT_INACTIVITY = timedelta(hours=24)
SHADOW_STATE_RETENTION = timedelta(days=30)
ClientFactory = Callable[[str], SpotifyCatalogClient]


@dataclass(frozen=True, slots=True)
class ShadowCycleSummary:
    refresh_source_count: int
    api_request_count: int
    playlist_count: int
    equivalent_playlist_count: int


class SpotifyFirstShadow:
    """Run Spotify-first ingestion/comparison without destination playlist operations."""

    def __init__(
        self,
        store: SQLiteStore,
        journal: SpotifyFirstJournal,
        detector_journal: PublicDetectorJournal,
        diagnostics: OperationalDiagnostics,
        auth: SpotifyBackoffAuthProvider,
        *,
        client_factory: ClientFactory | None = None,
        request_metrics: SpotifyRequestMetrics | None = None,
        inactivity: timedelta = DEFAULT_INACTIVITY,
        max_pages: int = DEFAULT_MAX_PAGES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if inactivity <= timedelta(0):
            raise ValueError("Spotify-first inactivity threshold must be positive")
        if max_pages <= 0:
            raise ValueError("Spotify-first max_pages must be positive")
        self.store = store
        self.journal = journal
        self.detector_journal = detector_journal
        self.diagnostics = diagnostics
        self.inactivity = inactivity
        self.max_pages = max_pages
        self.clock = clock or (lambda: datetime.now(UTC))

        rate_limit = SpotifyRateLimitJournal(store)
        self.auth = SpotifyRateLimitGuardAuth(
            auth,
            rate_limit,
            clock=self.clock,
            diagnostics=diagnostics,
        )
        metrics = request_metrics or SpotifyRequestMetrics(store.path)
        metrics.initialize()
        base_factory = client_factory or (
            lambda token: SpotifyClient(access_token=token, metrics=metrics)
        )

        def guarded_factory(token: str) -> SpotifyCatalogClient:
            return cast(
                SpotifyCatalogClient,
                SpotifyRateLimitGuardClient(
                    base_factory(token),
                    rate_limit,
                    clock=self.clock,
                    diagnostics=diagnostics,
                ),
            )

        self.client_factory = guarded_factory

    def run(
        self,
        config: EngineConfig,
        detector: PublicDetectorCycleSummary,
        *,
        observed_at: datetime,
    ) -> ShadowCycleSummary:
        now = as_utc(observed_at)
        plan = self._refresh_plan(config, detector, now=now)
        request_count = 0

        if plan:
            try:
                client = self.client_factory(self.auth.get_access_token(now=now))
            except (SpotifyAuthError, PersistenceError) as exc:
                emit_failure(
                    self.diagnostics,
                    "spotify_first_shadow_authorization_failed",
                    exc,
                    now=now,
                    phase="authorization",
                )
                return self._compare(config, now=now, refreshes=len(plan), requests=0)

            for source, reason, cutoff, public in plan:
                try:
                    snapshot = fetch_source_catalogue(
                        client,
                        source,
                        now=now,
                        cutoff=cutoff,
                        max_pages=self.max_pages,
                    )
                    counts = self.journal.upsert_episodes(snapshot.episodes)
                    self.journal.record_refresh(
                        source,
                        refreshed_at=now,
                        reason=reason,
                        detector_episode_id=public.last_episode_id,
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
                    emit_refresh(
                        self.diagnostics,
                        source,
                        reason,
                        public,
                        snapshot,
                        counts,
                        now=now,
                    )
                    emit_parser_fallbacks(
                        self.diagnostics,
                        source,
                        snapshot.episodes,
                        now=now,
                    )
                    emit_detector_correlation(
                        self.diagnostics,
                        source,
                        reason,
                        public,
                        snapshot,
                        now=now,
                    )
                except (
                    SpotifyApiError,
                    SpotifyTransportError,
                    PersistenceError,
                    MatchConfigurationError,
                    MatchResponseError,
                    ValueError,
                ) as exc:
                    emit_failure(
                        self.diagnostics,
                        "spotify_first_shadow_catalogue_refresh_failed",
                        exc,
                        now=now,
                        phase="verification",
                        source_id=source.id,
                        refresh_reason=reason,
                    )

            delegated = getattr(client, "show_episodes_delegated_count", None)
            if isinstance(delegated, int) and delegated >= 0:
                request_count = delegated

        try:
            self.journal.prune(before=now - SHADOW_STATE_RETENTION)
        except PersistenceError as exc:
            emit_failure(
                self.diagnostics,
                "spotify_first_shadow_prune_failed",
                exc,
                now=now,
                phase="retention",
            )
        return self._compare(config, now=now, refreshes=len(plan), requests=request_count)

    def _refresh_plan(
        self,
        config: EngineConfig,
        detector: PublicDetectorCycleSummary,
        *,
        now: datetime,
    ) -> list[tuple[SourceDefinition, RefreshReason, datetime, PublicDetectorState]]:
        changed = set(detector.changed_source_ids)
        plan: list[tuple[SourceDefinition, RefreshReason, datetime, PublicDetectorState]] = []
        for source in required_sources(config):
            show_id = spotify_show_id(source)
            public = self.detector_journal.get(source.id, show_id)
            if public is None or public.last_episode_id is None:
                continue
            cutoff = now - timedelta(hours=_max_retention(config, source.id))
            reason = _refresh_reason(
                source,
                show_id,
                public,
                self.journal.get_source_state(source.id),
                changed,
                now=now,
                inactivity=self.inactivity,
                cutoff=cutoff,
            )
            if reason is not None:
                plan.append((source, reason, cutoff, public))
        return plan

    def _compare(
        self,
        config: EngineConfig,
        *,
        now: datetime,
        refreshes: int,
        requests: int,
    ) -> ShadowCycleSummary:
        try:
            episodes = self.journal.list_episodes()
        except PersistenceError as exc:
            emit_failure(
                self.diagnostics,
                "spotify_first_shadow_comparison_failed",
                exc,
                now=now,
                phase="verification",
            )
            return ShadowCycleSummary(refreshes, requests, 0, 0)

        sources = {source.id: source for source in config.sources}
        playlist_count = 0
        equivalent_count = 0
        for playlist in config.playlists:
            if not playlist.enabled:
                continue
            playlist_count += 1
            try:
                shadow = build_shadow_desired_state(
                    playlist,
                    episodes,
                    sources=sources,
                    now=now,
                )
                legacy = build_legacy_desired_state(
                    self.store,
                    playlist,
                    now=now,
                    timezones={
                        source_id: source.timezone for source_id, source in sources.items()
                    },
                )
                valid = comparison_is_valid(
                    self.journal,
                    playlist,
                    sources,
                    now=now,
                )
                comparison = compare_desired_states(legacy, shadow, valid=valid)
            except (PersistenceError, DesiredStateError, ValueError, KeyError) as exc:
                emit_failure(
                    self.diagnostics,
                    "spotify_first_shadow_comparison_failed",
                    exc,
                    now=now,
                    phase="verification",
                    playlist_id=str(playlist.id),
                )
                continue
            if comparison.equivalent and comparison.valid:
                equivalent_count += 1
            emit_playlist_comparison(
                self.diagnostics,
                playlist,
                comparison,
                legacy.items,
                shadow,
                now=now,
            )

        self.diagnostics.emit(
            occurred_at=now,
            severity=DiagnosticSeverity.INFO,
            component="spotify.first_shadow",
            event_name="spotify_first_shadow_cycle_completed",
            details={
                "refresh_source_count": refreshes,
                "api_request_count": requests,
                "shadow_playlist_count": playlist_count,
                "equivalent_playlist_count": equivalent_count,
            },
        )
        return ShadowCycleSummary(refreshes, requests, playlist_count, equivalent_count)


def _refresh_reason(
    source: SourceDefinition,
    show_id: str,
    public: PublicDetectorState,
    local: SourceShadowState | None,
    changed: set[SourceId],
    *,
    now: datetime,
    inactivity: timedelta,
    cutoff: datetime,
) -> RefreshReason | None:
    if local is None or local.spotify_show_id != show_id:
        return RefreshReason.BOOTSTRAP
    if local.coverage_proven and local.coverage_cutoff_at > cutoff:
        return RefreshReason.BOOTSTRAP
    if public.last_change_at is not None and public.last_change_at > local.last_refresh_at:
        return RefreshReason.CHANGE
    if source.id in changed:
        return RefreshReason.CHANGE
    if public.audit_due(now=now, inactivity=inactivity):
        return RefreshReason.INACTIVITY_AUDIT
    return None


def _max_retention(config: EngineConfig, source_id: SourceId) -> int:
    values = [
        playlist.retention_hours
        for playlist in config.playlists
        if playlist.enabled and source_id in playlist.source_selection.explicit
    ]
    return max(values, default=48)
