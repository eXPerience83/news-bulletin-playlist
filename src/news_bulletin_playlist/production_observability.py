"""Production-only operational diagnostics for matching recovery and retention safety."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from news_bulletin_playlist.diagnostics import DiagnosticSeverity
from news_bulletin_playlist.effective_config import load_effective_config
from news_bulletin_playlist.engine import EngineCycleResult, SourceCycleOutcome
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.persistence import MatchStatus
from news_bulletin_playlist.spotify_metrics import SpotifyRequestScopeSnapshot
from news_bulletin_playlist.spotify_planner import (
    PlannerReason,
    SpotifyPlannerJournal,
    matched_recovery_planning_reason,
    matching_input_fingerprint,
)


class ProductionInstrumentedEngineCycleRunner(InstrumentedEngineCycleRunner):
    """Add bounded production evidence without changing engine/provider semantics."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._canonical_before_cycle: frozenset[tuple[str, str]] | None = None

    def run_cycle(self) -> EngineCycleResult:
        self._canonical_before_cycle = self._canonical_identity_snapshot()
        try:
            return super().run_cycle()
        finally:
            self._canonical_before_cycle = None

    def _emit_result(
        self,
        result: EngineCycleResult,
        cycle_id: str,
        *,
        request_snapshot: SpotifyRequestScopeSnapshot | None = None,
    ) -> None:
        self._emit_title_parse_diagnostics(result, cycle_id)
        self._emit_matching_recovery_diagnostics(result, cycle_id)
        self._emit_playlist_safety_diagnostics(result, cycle_id)
        self._emit_canonical_prune_diagnostics(result, cycle_id)
        super()._emit_result(result, cycle_id, request_snapshot=request_snapshot)

    def _emit_title_parse_diagnostics(
        self,
        result: EngineCycleResult,
        cycle_id: str,
    ) -> None:
        """Emit bounded mode counters without provider titles, ids, payloads or extra I/O."""
        for outcome in result.sources:
            collection = outcome.collection_diagnostics
            counts = {
                "title_parse_exact": collection.rss_title_parser_exact_count,
                "title_parse_normalized_recovered": (
                    collection.rss_title_parser_normalized_recovered_count
                ),
                "title_parse_provider_compat_recovered": (
                    collection.rss_title_parser_provider_compat_recovered_count
                ),
                "title_parse_rejected": collection.rss_title_parser_rejected_count,
            }
            for event_name, total in counts.items():
                if total is None or total <= 0:
                    continue
                self.diagnostics.emit(
                    occurred_at=result.finished_at,
                    severity=DiagnosticSeverity.INFO,
                    component="source.collection",
                    event_name=event_name,
                    cycle_id=cycle_id,
                    source_id=str(outcome.source_id),
                    details={"total": total},
                )

            if collection.failure_reason == "title_parser_drift":
                self.diagnostics.emit(
                    occurred_at=result.finished_at,
                    severity=DiagnosticSeverity.WARNING,
                    component="source.collection",
                    event_name="title_format_drift_unresolved",
                    cycle_id=cycle_id,
                    source_id=str(outcome.source_id),
                    details={"total": collection.rss_title_parser_rejected_count or 0},
                )

    def _emit_matching_recovery_diagnostics(
        self,
        result: EngineCycleResult,
        cycle_id: str,
    ) -> None:
        runtime = self._runtime_state()
        if runtime is None:
            return
        config, store, journal = runtime
        source_by_id = {source.id: source for source in config.sources}
        enabled_retention = [
            playlist.retention_hours for playlist in config.playlists if playlist.enabled
        ]
        matching_cutoff = result.finished_at - timedelta(hours=max(enabled_retention, default=48))

        for outcome in result.sources:
            attempted = outcome.matching_diagnostics.matching_reason_evidence_recovery_count
            if attempted > 0:
                self._emit_source_count(
                    result,
                    cycle_id,
                    outcome,
                    "matching_recovery_attempted",
                    attempted,
                    DiagnosticSeverity.INFO,
                )

            source = source_by_id.get(outcome.source_id)
            if source is None:
                continue
            counts = {
                "matching_recovery_backoff_active": 0,
                "matching_recovery_due_active": 0,
                "matching_recovery_untracked": 0,
                "matching_recovery_clock_rollback": 0,
                "matching_recovery_input_changed": 0,
                "matching_persistence_rejection_barrier_active": 0,
            }
            try:
                editions = store.list_editions(source_id=outcome.source_id)
                for edition in editions:
                    match = store.get_match_state(edition.source_id, edition.source_native_id)
                    if match is None:
                        continue

                    barrier = journal.get_persistence_rejection_barrier(
                        str(edition.source_id), edition.source_native_id
                    )
                    if barrier is not None and barrier.protects(match):
                        counts["matching_persistence_rejection_barrier_active"] += 1

                    if (
                        match.status is not MatchStatus.MATCHED
                        or edition.published_at < matching_cutoff
                        or edition.published_at > result.finished_at
                    ):
                        continue
                    match_input = journal.get_match_input(
                        str(edition.source_id), edition.source_native_id
                    )
                    if match_input is not None:
                        continue
                    recovery = journal.get_matched_recovery(
                        str(edition.source_id), edition.source_native_id
                    )
                    if recovery is None:
                        counts["matching_recovery_untracked"] += 1
                        continue
                    reason = matched_recovery_planning_reason(
                        recovery,
                        input_fingerprint=matching_input_fingerprint(source, edition),
                        now=result.finished_at,
                    )
                    if reason is None:
                        counts["matching_recovery_backoff_active"] += 1
                    elif reason is PlannerReason.RETRY_DUE:
                        counts["matching_recovery_due_active"] += 1
                    elif reason is PlannerReason.CLOCK_ROLLBACK:
                        counts["matching_recovery_clock_rollback"] += 1
                    elif reason is PlannerReason.INPUT_CHANGED:
                        counts["matching_recovery_input_changed"] += 1
            except Exception:  # noqa: BLE001 - diagnostics must never alter engine behavior
                continue

            for event_name, total in counts.items():
                if total <= 0:
                    continue
                severity = (
                    DiagnosticSeverity.INFO
                    if event_name == "matching_recovery_backoff_active"
                    else DiagnosticSeverity.WARNING
                )
                self._emit_source_count(
                    result,
                    cycle_id,
                    outcome,
                    event_name,
                    total,
                    severity,
                )

    def _emit_playlist_safety_diagnostics(
        self,
        result: EngineCycleResult,
        cycle_id: str,
    ) -> None:
        for playlist in result.playlists:
            if (
                playlist.ok
                and playlist.wrote is False
                and playlist.applied_count is not None
                and playlist.applied_count > playlist.desired_count
            ):
                self.diagnostics.emit(
                    occurred_at=result.finished_at,
                    severity=DiagnosticSeverity.INFO,
                    component="playlist.retention",
                    event_name="soft_retention_deferred",
                    cycle_id=cycle_id,
                    playlist_id=str(playlist.playlist_id),
                    details={"total": playlist.applied_count - playlist.desired_count},
                )

            event_name = _preservation_event_name(playlist.error)
            if event_name is not None:
                self.diagnostics.emit(
                    occurred_at=result.finished_at,
                    severity=DiagnosticSeverity.WARNING,
                    component="playlist.safety",
                    event_name=event_name,
                    cycle_id=cycle_id,
                    playlist_id=str(playlist.playlist_id),
                    details={"total": 1},
                )

    def _emit_canonical_prune_diagnostics(
        self,
        result: EngineCycleResult,
        cycle_id: str,
    ) -> None:
        before = self._canonical_before_cycle
        after = self._canonical_identity_snapshot()
        if before is None or after is None:
            return
        self.diagnostics.emit(
            occurred_at=result.finished_at,
            severity=DiagnosticSeverity.INFO,
            component="persistence.retention",
            event_name="canonical_editions_pruned",
            cycle_id=cycle_id,
            details={"total": len(before - after)},
        )

    def _emit_source_count(
        self,
        result: EngineCycleResult,
        cycle_id: str,
        source: SourceCycleOutcome,
        event_name: str,
        total: int,
        severity: DiagnosticSeverity,
    ) -> None:
        self.diagnostics.emit(
            occurred_at=result.finished_at,
            severity=severity,
            component="spotify.planner",
            event_name=event_name,
            cycle_id=cycle_id,
            source_id=str(source.source_id),
            details={"total": total},
        )

    def _runtime_state(self) -> tuple[Any, Any, Any] | None:
        runner = self.runner
        store = getattr(runner, "store", None)
        if store is None:
            return None
        try:
            config = getattr(runner, "config", None)
            if config is None:
                data_dir = getattr(runner, "data_dir", None)
                environ = getattr(runner, "environ", None)
                if data_dir is None or environ is None:
                    return None
                synchronization = getattr(runner, "configuration_synchronization", None)
                if synchronization is None:
                    config = load_effective_config(data_dir, environ)
                else:
                    with synchronization.hold():
                        config = load_effective_config(data_dir, environ)
            if config is None:
                return None
            journal = getattr(runner, "planner_journal", None)
            if journal is None:
                journal = SpotifyPlannerJournal(store)
            return config, store, journal
        except Exception:  # noqa: BLE001 - diagnostics are best-effort and read-only
            return None

    def _canonical_identity_snapshot(self) -> frozenset[tuple[str, str]] | None:
        store = getattr(self.runner, "store", None)
        if store is None:
            return None
        try:
            return frozenset(
                (str(edition.source_id), edition.source_native_id)
                for edition in store.list_editions()
            )
        except Exception:  # noqa: BLE001 - diagnostics must never fail the cycle
            return None


def _preservation_event_name(error: str | None) -> str | None:
    if error is None:
        return None
    persistence_rejection = (
        "destination preserved because a Spotify matching repair was rejected by "
        "durable persistence"
    )
    if error.startswith(persistence_rejection) or "temporally rejected matching repair" in error:
        return "destination_preserved_matching_persistence_rejection"
    if error == "destination preserved because planner persistence is unavailable":
        return "destination_preserved_planner_persistence_unavailable"
    if error == "destination preserved because final desired state could not be persisted":
        return "destination_preserved_desired_persistence_unavailable"
    if error == "destination preserved because local skip authority changed":
        return "destination_preserved_local_skip_authority_changed"
    if error.startswith("destination preserved because no last-known-good state is available"):
        return "destination_preserved_missing_last_known_good"
    return None
