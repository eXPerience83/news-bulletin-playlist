"""Translate engine outcomes into concise, sanitized operational diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from news_bulletin_playlist.desired_state import (
    DURATION_EXCEPTION,
    DURATION_EXCEEDS_DEFAULT_MAX,
    DURATION_EXCEEDS_EXCEPTION_MAX,
    DURATION_WITHIN_DEFAULT_MAX,
)
from news_bulletin_playlist.diagnostics import DiagnosticSeverity
from news_bulletin_playlist.engine import EngineCycleResult, EngineCycleRunner
from news_bulletin_playlist.reconciliation_diagnostics import classify_reconciliation_failure
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics, cycle_id_for

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
                },
            )

        for playlist in result.playlists:
            details: dict[str, str | int | bool | None] = {
                "desired_count": playlist.desired_count,
            }
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
                if playlist.error is not None:
                    details["verification_outcome"] = "degraded"
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
            for decision in playlist.duration_decisions:
                noteworthy_long_accept = (
                    decision.reason == DURATION_WITHIN_DEFAULT_MAX
                    and decision.accepted
                    and decision.duration_seconds >= _LONG_DURATION_DIAGNOSTIC_SECONDS
                )
                if decision.reason not in {
                    DURATION_EXCEPTION,
                    DURATION_EXCEEDS_DEFAULT_MAX,
                    DURATION_EXCEEDS_EXCEPTION_MAX,
                } and not noteworthy_long_accept:
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


def _destination_was_preserved(error: str | None) -> bool:
    return error is not None and error.startswith(
        "destination preserved because no last-known-good state is available"
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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("engine observability clock must be timezone-aware")
    return value.astimezone(UTC)
