"""Translate engine outcomes into concise, sanitized operational diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from news_bulletin_playlist.desired_state import (
    DURATION_EXCEEDS_DEFAULT_MAX,
    DURATION_EXCEEDS_EXCEPTION_MAX,
    DURATION_EXCEPTION,
    DURATION_WITHIN_DEFAULT_MAX,
    DurationEligibilityDecision,
)
from news_bulletin_playlist.diagnostics import DiagnosticSeverity
from news_bulletin_playlist.engine import EngineCycleResult, EngineCycleRunner
from news_bulletin_playlist.reconciliation_diagnostics import (
    classify_reconciliation_failure,
    classify_reconciliation_warning,
)
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics, cycle_id_for
from news_bulletin_playlist.spotify_backoff import (
    SPOTIFY_RATE_LIMIT_ACTIVE_PREFIX,
    SPOTIFY_RATE_LIMIT_PRESERVED_MESSAGE,
)
from news_bulletin_playlist.spotify_planner import PlannerReason

Clock = Callable[[], datetime]
_LONG_DURATION_DIAGNOSTIC_SECONDS = 20 * 60


class InstrumentedEngineCycleRunner:
    """Observe one engine runner without changing its collection/reconciliation semantics."""

    def __init__(
        self,
        runner: EngineCycleRunner,
        diagnostics: OperationalDiagnostics,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.runner = runner
        self.diagnostics = diagnostics
        self.clock = clock or _utc_now

    def run_cycle(self) -> EngineCycleResult:
        observed_start = _as_utc(self.clock())
        cycle_id = cycle_id_for(observed_start)
        self.diagnostics.emit(
            occurred_at=observed_start,
            severity=DiagnosticSeverity.INFO,
            component="engine",
            event_name="cycle_started",
            cycle_id=cycle_id,
        )
        try:
            result = self.runner.run_cycle()
        except Exception:  # noqa: BLE001 - preserve scheduler isolation; never log exception text
            self.diagnostics.emit(
                occurred_at=_as_utc(self.clock()),
                severity=DiagnosticSeverity.ERROR,
                component="engine",
                event_name="cycle_unexpected_failure",
                cycle_id=cycle_id,
                details={"phase": "scheduler"},
            )
            raise

        self._emit_result(result, cycle_id)
        return result

    def _emit_result(self, result: EngineCycleResult, cycle_id: str) -> None:
        for source in result.sources:
            if not source.collection_ok:
                event_name = "source_collection_failed"
                severity = DiagnosticSeverity.ERROR
                phase = "collection"
            elif source.matching_ok is False:
                event_name = "source_matching_failed"
                severity = DiagnosticSeverity.ERROR
                phase = "matching"
            elif source.matching_ok is None:
                event_name = "source_matching_deferred"
                severity = DiagnosticSeverity.WARNING
                phase = "matching"
            else:
                event_name = "source_cycle_completed"
                severity = DiagnosticSeverity.INFO
                phase = "complete"
            self.diagnostics.emit(
                occurred_at=result.finished_at,
                severity=severity,
                component="source",
                event_name=event_name,
                cycle_id=cycle_id,
                source_id=str(source.source_id),
                details={
                    "edition_count": source.edition_count,
                    "matched_count": source.matched_count,
                    "phase": phase,
                    "matching_work_count": source.matching_diagnostics.matching_work_count,
                    "matching_reason_new_match_count": (
                        source.matching_diagnostics.matching_reason_new_match_count
                    ),
                    "matching_reason_input_changed_count": (
                        source.matching_diagnostics.matching_reason_input_changed_count
                    ),
                    "matching_reason_retry_due_count": (
                        source.matching_diagnostics.matching_reason_retry_due_count
                    ),
                    "matching_reason_coalesced_unresolved_count": (
                        source.matching_diagnostics.matching_reason_coalesced_unresolved_count
                    ),
                    "matching_reason_evidence_recovery_count": (
                        source.matching_diagnostics.matching_reason_evidence_recovery_count
                    ),
                    "matching_reason_missing_evidence_count": (
                        source.matching_diagnostics.matching_reason_missing_evidence_count
                    ),
                    "unresolved_relevant_count": (
                        source.matching_diagnostics.unresolved_relevant_count
                    ),
                    "catalogue_audit_due": source.matching_diagnostics.catalogue_audit_due,
                    "matching_plan_conservative": (
                        source.matching_diagnostics.matching_plan_conservative
                    ),
                    "catalogue_requested": source.matching_diagnostics.catalogue_requested,
                    "catalogue_inspected": source.matching_diagnostics.catalogue_inspected,
                    "catalogue_request_count": (
                        source.matching_diagnostics.catalogue_request_count
                    ),
                },
            )

        for playlist in result.playlists:
            details: dict[str, str | int | bool | None] = {
                "desired_count": playlist.desired_count,
            }
            if playlist.request_item_count is not None:
                details["provider"] = "spotify"
                details["service"] = "spotify"
            optional_details = {
                "request_item_count": playlist.request_item_count,
                "request_payload_bytes": playlist.request_payload_bytes,
                "valid_uri_count": playlist.valid_uri_count,
                "invalid_uri_count": playlist.invalid_uri_count,
                "duplicate_count": playlist.duplicate_count,
                "desired_fingerprint": playlist.desired_fingerprint,
                "desired_changed_since_previous_attempt": (
                    playlist.desired_changed_since_previous_attempt
                ),
                "consecutive_failure_count": playlist.consecutive_failure_count,
                "first_failure_at": _optional_diagnostic_timestamp(playlist.first_failure_at),
                "last_failure_at": _optional_diagnostic_timestamp(playlist.last_failure_at),
                "write_outcome": playlist.write_outcome,
            }
            details.update(
                (key, value) for key, value in optional_details.items() if value is not None
            )
            if playlist.applied_count is not None:
                details["applied_count"] = playlist.applied_count

            if playlist.ok:
                event_name = "playlist_reconciled"
                severity = (
                    DiagnosticSeverity.WARNING
                    if playlist.error is not None
                    else DiagnosticSeverity.INFO
                )
                details["write_decision"] = "applied" if playlist.wrote else "unchanged"
                if playlist.request_item_count is not None:
                    details.setdefault(
                        "write_outcome", "verified" if playlist.wrote else "not_attempted"
                    )
                if playlist.error is not None:
                    details["verification_outcome"] = "degraded"
                    warning_reason = classify_reconciliation_warning(playlist.error)
                    if warning_reason is not None:
                        details["failure_reason"] = warning_reason
            elif _destination_was_preserved(playlist.error):
                event_name = "destination_preserved"
                severity = DiagnosticSeverity.WARNING
                details["write_decision"] = "preserved"
            else:
                event_name = "playlist_reconciliation_failed"
                severity = DiagnosticSeverity.ERROR
                details.update(classify_reconciliation_failure(playlist.error).details())

            self.diagnostics.emit(
                occurred_at=result.finished_at,
                severity=severity,
                component="playlist",
                event_name=event_name,
                cycle_id=cycle_id,
                playlist_id=str(playlist.playlist_id),
                details=details,
            )
            self._emit_duration_profiles(
                playlist.duration_decisions,
                playlist_id=str(playlist.playlist_id),
                cycle_id=cycle_id,
                occurred_at=result.finished_at,
            )
            for decision in playlist.duration_decisions:
                noteworthy_long_accept = (
                    decision.reason == DURATION_WITHIN_DEFAULT_MAX
                    and decision.accepted
                    and decision.duration_seconds >= _LONG_DURATION_DIAGNOSTIC_SECONDS
                )
                if (
                    decision.reason
                    not in {
                        DURATION_EXCEPTION,
                        DURATION_EXCEEDS_DEFAULT_MAX,
                        DURATION_EXCEEDS_EXCEPTION_MAX,
                    }
                    and not noteworthy_long_accept
                ):
                    continue
                policy_details: dict[str, str | int | bool | None] = {
                    "duration_seconds": decision.duration_seconds,
                    "eligibility_reason": decision.reason,
                    "max_seconds": decision.max_seconds,
                }
                if decision.exception_id is not None:
                    policy_details["policy_exception"] = decision.exception_id
                if noteworthy_long_accept:
                    duration_event = "duration_long_episode_accepted"
                elif decision.accepted:
                    duration_event = "duration_exception_applied"
                else:
                    duration_event = "duration_episode_excluded"
                self.diagnostics.emit(
                    occurred_at=result.finished_at,
                    severity=DiagnosticSeverity.INFO,
                    component="playlist.eligibility",
                    event_name=duration_event,
                    cycle_id=cycle_id,
                    source_id=str(decision.source_id),
                    playlist_id=str(playlist.playlist_id),
                    details=policy_details,
                )

        shadow = result.planner_shadow
        if shadow is not None:
            reason_set = set(shadow.reasons)
            details = {
                "planner_shadow_enabled": True,
                "planner_would_need_spotify": shadow.would_need_spotify,
                "planner_would_allow_local_skip": shadow.would_allow_local_skip,
                "planner_matching_work_count": shadow.matching_work_count,
                "planner_matching_source_count": shadow.matching_source_count,
                "planner_dirty_destination_count": shadow.dirty_destination_count,
                "planner_catalogue_audit_due_count": shadow.catalogue_audit_due_count,
                "planner_destination_audit_due_count": shadow.destination_audit_due_count,
                "planner_safety_blocker_count": shadow.safety_blocker_count,
                "planner_state_available": shadow.state_available,
                "provider_work_planned": result.provider_work_planned,
                "provider_session_created": result.provider_session_created,
                "destinations_skipped": result.destinations_skipped,
                "destinations_remote": result.destinations_remote,
                "destinations_preserved": result.destinations_preserved,
            }
            details.update(
                {f"planner_reason_{reason.value}": reason in reason_set for reason in PlannerReason}
            )
            self.diagnostics.emit(
                occurred_at=result.finished_at,
                severity=DiagnosticSeverity.INFO,
                component="spotify.planner",
                event_name="spotify_planner_shadow_decision",
                cycle_id=cycle_id,
                details=details,
            )

        duration_ms = max(
            round((result.finished_at - result.started_at).total_seconds() * 1000),
            0,
        )
        if result.ok:
            severity = DiagnosticSeverity.INFO
            event_name = "cycle_completed"
            phase = "complete"
        elif result.sources or result.playlists:
            severity = DiagnosticSeverity.WARNING
            event_name = "cycle_degraded"
            phase = "complete"
        else:
            severity = DiagnosticSeverity.ERROR
            event_name = "cycle_failed"
            phase = _cycle_failure_phase(result.error)
        self.diagnostics.emit(
            occurred_at=result.finished_at,
            severity=severity,
            component="engine",
            event_name=event_name,
            cycle_id=cycle_id,
            details={
                "duration_ms": duration_ms,
                "phase": phase,
                "total": len(result.sources) + len(result.playlists),
            },
        )

    def _emit_duration_profiles(
        self,
        decisions: tuple[DurationEligibilityDecision, ...],
        *,
        playlist_id: str,
        cycle_id: str,
        occurred_at: datetime,
    ) -> None:
        """Emit one bounded histogram per source instead of logging every short accepted episode."""
        by_source: dict[str, list[DurationEligibilityDecision]] = {}
        for decision in decisions:
            by_source.setdefault(str(decision.source_id), []).append(decision)

        for source_id, source_decisions in sorted(by_source.items()):
            buckets = {
                "lt_5m": 0,
                "m5_lt8": 0,
                "m8_lt10": 0,
                "m10_lt15": 0,
                "m15_lt20": 0,
                "m20_le30": 0,
                "gt_30m": 0,
            }
            accepted = 0
            for decision in source_decisions:
                duration = decision.duration_seconds
                if duration < 300:
                    buckets["lt_5m"] += 1
                elif duration < 480:
                    buckets["m5_lt8"] += 1
                elif duration < 600:
                    buckets["m8_lt10"] += 1
                elif duration < 900:
                    buckets["m10_lt15"] += 1
                elif duration < 1200:
                    buckets["m15_lt20"] += 1
                elif duration <= 1800:
                    buckets["m20_le30"] += 1
                else:
                    buckets["gt_30m"] += 1
                accepted += int(decision.accepted)

            self.diagnostics.emit(
                occurred_at=occurred_at,
                severity=DiagnosticSeverity.INFO,
                component="playlist.eligibility",
                event_name="duration_profile",
                cycle_id=cycle_id,
                source_id=source_id,
                playlist_id=playlist_id,
                details={
                    "sample_count": len(source_decisions),
                    "accepted_count": accepted,
                    "excluded_count": len(source_decisions) - accepted,
                    **buckets,
                },
            )


def _destination_was_preserved(error: str | None) -> bool:
    if error is None:
        return False
    return (
        error.startswith("destination preserved because no last-known-good state is available")
        or error.startswith(SPOTIFY_RATE_LIMIT_ACTIVE_PREFIX)
        or error == SPOTIFY_RATE_LIMIT_PRESERVED_MESSAGE
    )


def _cycle_failure_phase(error: str | None) -> str:
    if error is None:
        return "complete"
    if error.startswith("production engine configuration"):
        return "configuration"
    if error.startswith("unexpected engine failure"):
        return "scheduler"
    if "authorization" in error.casefold():
        return "authorization"
    return "persistence"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _optional_diagnostic_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("engine observability clock must be timezone-aware")
    return value.astimezone(UTC)
