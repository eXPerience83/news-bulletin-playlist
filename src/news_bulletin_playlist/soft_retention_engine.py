"""Fail-closed engine integration for per-playlist soft retention."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from news_bulletin_playlist.desired_state import DesiredPlaylistState, DesiredStateError
from news_bulletin_playlist.engine import (
    EngineCycleResult,
    EngineRunner,
    _DestinationWorkPlan,
)
from news_bulletin_playlist.models import PlaylistDefinition, PlaylistId, SourceDefinition, SourceId
from news_bulletin_playlist.persistence import MatchStatus, PersistenceError, SQLiteStore
from news_bulletin_playlist.reconciliation import build_desired_state_from_store
from news_bulletin_playlist.snapshot_pending import PendingSnapshotJournal
from news_bulletin_playlist.soft_retention import SoftRetentionDecision, classify_soft_retention
from news_bulletin_playlist.soft_retention_context import bind_soft_retention_audit_resolver
from news_bulletin_playlist.spotify_destination_audit import playlist_attestation_fingerprint
from news_bulletin_playlist.spotify_planner import (
    DestinationState,
    DestinationVerificationContext,
    PlannerShadowResult,
    SpotifyPlannerJournal,
    destination_context_fingerprint,
    matching_input_fingerprint,
    playlist_config_fingerprint,
    stable_fingerprint,
)


@dataclass(frozen=True, slots=True)
class SoftRetentionEvidence:
    physical_baseline: DesiredPlaylistState
    decision: SoftRetentionDecision


def resolve_soft_retention_evidence(
    store: SQLiteStore,
    sources: Mapping[SourceId, SourceDefinition],
    playlist: PlaylistDefinition,
    logical_desired: DesiredPlaylistState,
    *,
    now: datetime,
    auth_context_fingerprint: str,
) -> SoftRetentionEvidence | None:
    """Prove that one playlist delta consists only of age-based removals.

    The existing strong playlist attestation is treated as the physical baseline. Rebuilding the
    state at the attestation timestamp avoids persisting another URI list. Any missing, pending,
    remapped, context-invalid or otherwise contradictory evidence rejects the optimization.
    """
    observed_at = _as_utc(now)
    try:
        if logical_desired.playlist_id != playlist.id:
            return None
        if PendingSnapshotJournal(store).get(playlist.id) is not None:
            return None

        attestation = store.get_playlist_attestation(playlist.id)
        if attestation is None or attestation.destination_id != playlist.destination.external_id:
            return None

        journal = SpotifyPlannerJournal(store)
        destination = journal.get_destination(str(playlist.id))
        if (
            destination is None
            or destination.dirty
            or destination.unknown
            or destination.confirmed_desired_fingerprint is None
            or destination.confirmed_desired_fingerprint != destination.observed_desired_fingerprint
            or destination.last_verified_at is None
            or observed_at < destination.last_verified_at
            or destination.destination_context_fingerprint
            != destination_context_fingerprint(playlist)
            or destination.config_fingerprint != playlist_config_fingerprint(playlist)
            or destination.auth_context_fingerprint != auth_context_fingerprint
        ):
            return None

        selected_sources: dict[SourceId, SourceDefinition] = {}
        for source_id in playlist.source_selection.explicit:
            source = sources.get(source_id)
            if source is None:
                return None
            selected_sources[source_id] = source

        baseline = build_desired_state_from_store(
            store,
            playlist,
            now=attestation.updated_at,
            source_timezones={
                source_id: source.timezone for source_id, source in selected_sources.items()
            },
        )
        baseline_fingerprint = stable_fingerprint(baseline.uris)
        if (
            playlist_attestation_fingerprint(baseline.uris) != attestation.desired_fingerprint
            or baseline_fingerprint != destination.confirmed_desired_fingerprint
        ):
            return None

        for item in baseline.items:
            edition = store.get_edition(item.source_id, item.source_native_id)
            match = store.get_match_state(item.source_id, item.source_native_id)
            source = selected_sources.get(item.source_id)
            if (
                edition is None
                or match is None
                or source is None
                or match.status is not MatchStatus.MATCHED
                or match.spotify_episode_uri != item.spotify_episode_uri
                or match.updated_at > attestation.updated_at
            ):
                return None
            match_input = journal.get_match_input(str(item.source_id), item.source_native_id)
            if match_input is None or match_input.input_fingerprint != matching_input_fingerprint(
                source, edition
            ):
                return None

        decision = classify_soft_retention(
            playlist,
            baseline,
            logical_desired,
            now=observed_at,
        )
        if not decision.expiry_only:
            return None
        return SoftRetentionEvidence(baseline, decision)
    except DesiredStateError, PersistenceError, ValueError:
        return None


class _SoftRetentionPlannerJournal(SpotifyPlannerJournal):
    def __init__(self, store: SQLiteStore, runner: SoftRetentionEngineRunner) -> None:
        super().__init__(store)
        self.runner = runner

    def observe_destination(
        self,
        *,
        playlist_id: str,
        destination_context_fingerprint: str,
        config_fingerprint: str,
        auth_context_fingerprint: str,
        desired_fingerprint: str,
    ) -> DestinationState:
        effective = self.runner._soft_retention_effective_fingerprint(
            playlist_id,
            desired_fingerprint,
        )
        return super().observe_destination(
            playlist_id=playlist_id,
            destination_context_fingerprint=destination_context_fingerprint,
            config_fingerprint=config_fingerprint,
            auth_context_fingerprint=auth_context_fingerprint,
            desired_fingerprint=effective,
        )


class SoftRetentionEngineRunner(EngineRunner):
    """Engine runner that defers only strongly proven expiry-only destination deltas."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._soft_retention_now: datetime | None = None
        self.planner_journal = _SoftRetentionPlannerJournal(self.store, self)

    def _run_cycle(self) -> EngineCycleResult:
        with bind_soft_retention_audit_resolver(self._soft_retention_audit_desired):
            return super()._run_cycle()

    def _compute_shadow_plan(self, now: datetime) -> PlannerShadowResult:
        self._soft_retention_now = _as_utc(now)
        return super()._compute_shadow_plan(now)

    def _plan_destinations(
        self,
        now: datetime,
        unsafe_source_ids: set[SourceId],
    ) -> tuple[set[PlaylistId], set[PlaylistId]]:
        self._soft_retention_now = _as_utc(now)
        return super()._plan_destinations(now, unsafe_source_ids)

    def _destination_work_plan(
        self,
        playlist: PlaylistDefinition,
        now: datetime,
        expected: DestinationVerificationContext,
    ) -> _DestinationWorkPlan:
        self._soft_retention_now = _as_utc(now)
        plan = super()._destination_work_plan(playlist, now, expected)
        if not plan.state_available or not plan.needs_remote or plan.audit_only:
            return plan
        try:
            logical_desired = build_desired_state_from_store(
                self.store,
                playlist,
                now=now,
                source_timezones=self._source_timezones(),
            )
        except DesiredStateError, PersistenceError, ValueError:
            return plan
        evidence = self._soft_retention_evidence(playlist, logical_desired, now=now)
        if evidence is None or not evidence.decision.should_defer:
            return plan
        # A restart intentionally loses the six-hour runtime lease. Verify the physical baseline
        # snapshot-first instead of turning that safety check into an expiry-only mutation.
        return replace(plan, audit_only=True)

    def _destination_verification_context(
        self,
        playlist: PlaylistDefinition,
        desired_uris: tuple[str, ...],
    ) -> DestinationVerificationContext | None:
        expected = super()._destination_verification_context(playlist, desired_uris)
        if expected is None:
            return None
        effective = self._soft_retention_effective_fingerprint(
            str(playlist.id),
            expected.desired_fingerprint,
        )
        if effective == expected.desired_fingerprint:
            return expected
        return replace(expected, desired_fingerprint=effective)

    def _soft_retention_effective_fingerprint(
        self,
        playlist_id: str,
        requested_fingerprint: str,
    ) -> str:
        now = self._soft_retention_now
        auth_context = self._shadow_auth_context
        if now is None or auth_context is None:
            return requested_fingerprint
        playlist = next(
            (
                candidate
                for candidate in self.config.playlists
                if candidate.enabled and str(candidate.id) == playlist_id
            ),
            None,
        )
        if playlist is None:
            return requested_fingerprint
        try:
            logical_desired = build_desired_state_from_store(
                self.store,
                playlist,
                now=now,
                source_timezones=self._source_timezones(),
            )
        except DesiredStateError, PersistenceError, ValueError:
            return requested_fingerprint
        if stable_fingerprint(logical_desired.uris) != requested_fingerprint:
            return requested_fingerprint
        evidence = self._soft_retention_evidence(playlist, logical_desired, now=now)
        if evidence is None or not evidence.decision.should_defer:
            return requested_fingerprint
        return stable_fingerprint(evidence.physical_baseline.uris)

    def _soft_retention_evidence(
        self,
        playlist: PlaylistDefinition,
        logical_desired: DesiredPlaylistState,
        *,
        now: datetime,
    ) -> SoftRetentionEvidence | None:
        auth_context = self._shadow_auth_context
        if auth_context is None:
            return None
        return resolve_soft_retention_evidence(
            self.store,
            {source.id: source for source in self.config.sources},
            playlist,
            logical_desired,
            now=now,
            auth_context_fingerprint=auth_context,
        )

    def _soft_retention_audit_desired(
        self,
        store: SQLiteStore,
        playlist: PlaylistDefinition,
        logical_desired: DesiredPlaylistState,
        observed_at: datetime,
    ) -> DesiredPlaylistState | None:
        if Path(store.path).resolve() != Path(self.store.path).resolve():
            return None
        evidence = self._soft_retention_evidence(
            playlist,
            logical_desired,
            now=observed_at,
        )
        if evidence is None or not evidence.decision.should_defer:
            return None
        return evidence.physical_baseline


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("soft-retention timestamps must be timezone-aware")
    return value.astimezone(UTC)
