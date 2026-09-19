"""Translate Spotify-first engine outcomes into bounded operational diagnostics."""

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
from news_bulletin_playlist.engine import EngineCycleResult, EngineCycleRunner, SourceCycleOutcome
from news_bulletin_playlist.reconciliation_diagnostics import (
    classify_reconciliation_failure,
    classify_reconciliation_warning,
)
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics, cycle_id_for
from news_bulletin_playlist.spotify_metrics import (
    SpotifyRequestMetrics,
    SpotifyRequestScopeSnapshot,
)

Clock = Callable[[], datetime]
_LONG_DURATION_DIAGNOSTIC_SECONDS = 20 * 60


class InstrumentedEngineCycleRunner:
    """Observe the authoritative Spotify-first engine without changing its semantics."""

    def __init__(
        self,
        runner: EngineCycleRunner,
        diagnostics: OperationalDiagnostics,
        *,
        clock: Clock | None = None,
        request_metrics: SpotifyRequestMetrics | None = None,
    ) -> None:
        self.runner = runner
        self.diagnostics = diagnostics
        self.clock = clock or _utc_now
        discovered = getattr(runner, "request_metrics", None)
        self.request_metrics = (
            request_metrics
            if request_metrics is not None
            else discovered
            if isinstance(discovered, SpotifyRequestMetrics)
            else None
        )

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

        metrics = self.request_metrics
        if metrics is None:
            result = self._run_guarded(cycle_id)
            request_snapshot = None
        else:
            with metrics.request_scope() as request_scope:
                try:
                    result = self.runner.run_cycle()
                except Exception:  # noqa: BLE001 - scheduler isolation; never log exception text
                    occurred_at = _as_utc(self.clock())
                    self._emit_spotify_request_scope(
                        request_scope.snapshot(),
                        cycle_id=cycle_id,
                        occurred_at=occurred_at,
                    )
                    self._emit_unexpected(cycle_id, occurred_at)
                    raise
                request_snapshot = request_scope.snapshot()

        self._emit_result(result, cycle_id, request_snapshot=request_snapshot)
        return result

    def _run_guarded(self, cycle_id: str) -> EngineCycleResult:
        try:
            return self.runner.run_cycle()
        except Exception:  # noqa: BLE001 - scheduler isolation; never log exception text
            self._emit_unexpected(cycle_id, _as_utc(self.clock()))
            raise

    def _emit_unexpected(self, cycle_id: str, occurred_at: datetime) -> None:
        self.diagnostics.emit(
            occurred_at=occurred_at,
            severity=DiagnosticSeverity.ERROR,
            component="engine",
            event_name="cycle_unexpected_failure",
            cycle_id=cycle_id,
            details={"phase": "scheduler"},
        )

    def _emit_result(
        self,
        result: EngineCycleResult,
        cycle_id: str,
        *,
        request_snapshot: SpotifyRequestScopeSnapshot | None,
    ) -> None:
        for source in result.sources:
            self._emit_source(source, result.finished_at, cycle_id)
        for playlist in result.playlists:
            self._emit_playlist(playlist, result.finished_at, cycle_id)

        if request_snapshot is not None:
            self._emit_spotify_request_scope(
                request_snapshot,
                cycle_id=cycle_id,
                occurred_at=result.finished_at,
            )

        duration_ms = max(
            round((result.finished_at - result.started_at).total_seconds() * 1000),
            0,
        )
        if result.ok:
            severity = DiagnosticSeverity.INFO
            event_name = "cycle_completed"
        elif result.sources or result.playlists:
            severity = DiagnosticSeverity.WARNING
            event_name = "cycle_degraded"
        else:
            severity = DiagnosticSeverity.ERROR
            event_name = "cycle_failed"
        self.diagnostics.emit(
            occurred_at=result.finished_at,
            severity=severity,
            component="engine",
            event_name=event_name,
            cycle_id=cycle_id,
            details={
                "duration_ms": duration_ms,
                "phase": "complete" if result.sources or result.playlists else "scheduler",
                "total": len(result.sources) + len(result.playlists),
            },
        )

    def _emit_source(
        self,
        source: SourceCycleOutcome,
        occurred_at: datetime,
        cycle_id: str,
    ) -> None:
        if source.detector_false_negative:
            severity = DiagnosticSeverity.WARNING
            event_name = "source_detector_false_negative"
        elif not source.ok:
            severity = DiagnosticSeverity.WARNING
            event_name = "source_state_preserved"
        else:
            severity = DiagnosticSeverity.INFO
            event_name = "source_cycle_completed"

        details: dict[str, str | int | bool | None] = {
            "episode_count": source.episode_count,
            "api_request_count": source.api_request_count,
            "refreshed": source.refreshed,
            "detector_ok": source.detector_ok,
            "detector_changed": source.detector_changed,
            "detector_false_negative": source.detector_false_negative,
            "parser_fallback_count": source.parser_fallback_count,
            "filter_rejected_count": source.filter_rejected_count,
            "removed_count": source.removed_count,
            "phase": "complete",
        }
        if source.refresh_reason is not None:
            details["refresh_reason"] = source.refresh_reason

        self.diagnostics.emit(
            occurred_at=occurred_at,
            severity=severity,
            component="source",
            event_name=event_name,
            cycle_id=cycle_id,
            source_id=str(source.source_id),
            details=details,
        )

    def _emit_playlist(
        self,
        playlist,  # type: ignore[no-untyped-def]
        occurred_at: datetime,
        cycle_id: str,
    ) -> None:
        details: dict[str, str | int | bool | None] = {
            "desired_count": playlist.desired_count,
            "duration_unavailable_count": playlist.duration_unavailable_count,
            "local_skip": playlist.local_skip,
            "stale_escalation": playlist.stale_escalation,
        }
        optional = {
            "applied_count": playlist.applied_count,
            "request_item_count": playlist.request_item_count,
            "request_payload_bytes": playlist.request_payload_bytes,
            "valid_uri_count": playlist.valid_uri_count,
            "invalid_uri_count": playlist.invalid_uri_count,
            "duplicate_count": playlist.duplicate_count,
            "desired_fingerprint": playlist.desired_fingerprint,
            "write_outcome": playlist.write_outcome,
        }
        details.update((key, value) for key, value in optional.items() if value is not None)
        if playlist.request_item_count is not None:
            details["provider"] = "spotify"
            details["service"] = "spotify"

        if playlist.ok:
            severity = DiagnosticSeverity.WARNING if playlist.error else DiagnosticSeverity.INFO
            event_name = "playlist_reconciled"
            if playlist.local_skip:
                details["write_decision"] = "skipped"
            else:
                details["write_decision"] = "applied" if playlist.wrote else "unchanged"
            if playlist.error is not None:
                details["verification_outcome"] = "degraded"
                warning = classify_reconciliation_warning(playlist.error)
                if warning is not None:
                    details["failure_reason"] = warning
        elif _destination_was_preserved(playlist.error):
            severity = DiagnosticSeverity.WARNING
            event_name = "destination_preserved"
            details["write_decision"] = "preserved"
        else:
            severity = DiagnosticSeverity.ERROR
            event_name = "playlist_reconciliation_failed"
            details.update(classify_reconciliation_failure(playlist.error).details())

        self.diagnostics.emit(
            occurred_at=occurred_at,
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
            occurred_at=occurred_at,
        )
        self._emit_noteworthy_duration_decisions(
            playlist.duration_decisions,
            playlist_id=str(playlist.playlist_id),
            cycle_id=cycle_id,
            occurred_at=occurred_at,
        )

    def _emit_noteworthy_duration_decisions(
        self,
        decisions: tuple[DurationEligibilityDecision, ...],
        *,
        playlist_id: str,
        cycle_id: str,
        occurred_at: datetime,
    ) -> None:
        for decision in decisions:
            long_accept = (
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
                and not long_accept
            ):
                continue

            details: dict[str, str | int | bool | None] = {
                "duration_seconds": decision.duration_seconds,
                "eligibility_reason": decision.reason,
                "max_seconds": decision.max_seconds,
            }
            if decision.exception_id is not None:
                details["policy_exception"] = decision.exception_id
            event_name = (
                "duration_long_episode_accepted"
                if long_accept
                else "duration_exception_applied"
                if decision.accepted
                else "duration_episode_excluded"
            )
            self.diagnostics.emit(
                occurred_at=occurred_at,
                severity=DiagnosticSeverity.INFO,
                component="playlist.eligibility",
                event_name=event_name,
                cycle_id=cycle_id,
                source_id=str(decision.source_id),
                playlist_id=playlist_id,
                details=details,
            )

    def _emit_duration_profiles(
        self,
        decisions: tuple[DurationEligibilityDecision, ...],
        *,
        playlist_id: str,
        cycle_id: str,
        occurred_at: datetime,
    ) -> None:
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

    def _emit_spotify_request_scope(
        self,
        snapshot: SpotifyRequestScopeSnapshot,
        *,
        cycle_id: str,
        occurred_at: datetime,
    ) -> None:
        self.diagnostics.emit(
            occurred_at=occurred_at,
            severity=DiagnosticSeverity.INFO,
            component="spotify.requests",
            event_name="cycle_request_accounting",
            cycle_id=cycle_id,
            details={"total": snapshot.total_requests},
        )
        if snapshot.web_api_requests:
            self.diagnostics.emit(
                occurred_at=occurred_at,
                severity=DiagnosticSeverity.INFO,
                component="spotify.requests",
                event_name="cycle_request_web_api_total",
                cycle_id=cycle_id,
                details={"total": snapshot.web_api_requests},
            )
        for operation, count in snapshot.web_api:
            if count > 0:
                self.diagnostics.emit(
                    occurred_at=occurred_at,
                    severity=DiagnosticSeverity.INFO,
                    component="spotify.requests",
                    event_name=f"cycle_request_web_api_{operation.value}",
                    cycle_id=cycle_id,
                    details={"total": count},
                )
        if snapshot.oauth_requests:
            self.diagnostics.emit(
                occurred_at=occurred_at,
                severity=DiagnosticSeverity.INFO,
                component="spotify.requests",
                event_name="cycle_request_oauth_total",
                cycle_id=cycle_id,
                details={"total": snapshot.oauth_requests},
            )
        for operation, count in snapshot.oauth:
            if count > 0:
                self.diagnostics.emit(
                    occurred_at=occurred_at,
                    severity=DiagnosticSeverity.INFO,
                    component="spotify.requests",
                    event_name=f"cycle_request_oauth_{operation.value}",
                    cycle_id=cycle_id,
                    details={"total": count},
                )
        if snapshot.http_rate_limits:
            self.diagnostics.emit(
                occurred_at=occurred_at,
                severity=DiagnosticSeverity.WARNING,
                component="spotify.requests",
                event_name="cycle_request_http_429",
                cycle_id=cycle_id,
                details={"total": snapshot.http_rate_limits},
            )
        if snapshot.quota_exceeded:
            self.diagnostics.emit(
                occurred_at=occurred_at,
                severity=DiagnosticSeverity.WARNING,
                component="spotify.requests",
                event_name="cycle_request_quota_exceeded",
                cycle_id=cycle_id,
                details={"total": snapshot.quota_exceeded},
            )


def _destination_was_preserved(error: str | None) -> bool:
    if error is None:
        return False
    folded = error.casefold()
    return "preserved" in folded or "cooldown" in folded or "rate-limit" in folded


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("engine observability clock must be timezone-aware")
    return value.astimezone(UTC)
