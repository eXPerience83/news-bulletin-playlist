"""Convert reconciliation failures into bounded, non-secret diagnostic fields."""

from __future__ import annotations

import re
from dataclasses import dataclass

from news_bulletin_playlist.diagnostics import DiagnosticValue

_API_ERROR = re.compile(r"^Spotify API (?P<status>\d{3}):")
_OPERATION_FAILURE = re.compile(
    r"^Spotify playlist (?P<phase>prewrite|readback|verification|write) "
    r"(?P<operation>playlist_items|replace_items|snapshot) "
    r"(?P<kind>API|transport) failure"
    r"(?: \(http_status=(?P<status>\d{3})\))?"
    r"(?: \(write_outcome=(?P<write_outcome>unknown)\))?$"
)
_OFFSET = re.compile(r"(?:^|[ (])offset=(?P<value>\d+)(?:[ )]|$)")
_RETURNED = re.compile(r"(?:^|[ (])returned=(?P<value>\d+)(?:[ )]|$)")
_TOTAL = re.compile(r"(?:^|[ (])total=(?P<value>\d+)(?:[ )]|$)")
_UNAVAILABLE_INDICES = re.compile(r"index/indices \[(?P<values>[0-9,]*)\]")
_SNAPSHOT_PROPAGATION_PREFIX = "Spotify verification degraded: snapshot propagation pending"


@dataclass(frozen=True, slots=True)
class ReconciliationFailureDiagnostic:
    """Safe classification that deliberately contains no provider/error text."""

    failure_class: str
    phase: str
    failure_reason: str = "unknown_reconciliation_error"
    operation: str | None = None
    offset: int | None = None
    returned_count: int | None = None
    total: int | None = None
    next_state: str | None = None
    unavailable_count: int | None = None
    verification_outcome: str | None = None
    http_status: int | None = None
    write_decision: str | None = None
    eligibility_reason: str | None = None
    write_outcome: str | None = None

    def details(self) -> dict[str, DiagnosticValue]:
        result: dict[str, DiagnosticValue] = {
            "failure_class": self.failure_class,
            "failure_reason": self.failure_reason,
            "phase": self.phase,
        }
        optional: tuple[tuple[str, DiagnosticValue], ...] = (
            ("operation", self.operation),
            ("offset", self.offset),
            ("returned_count", self.returned_count),
            ("total", self.total),
            ("next_state", self.next_state),
            ("unavailable_count", self.unavailable_count),
            ("verification_outcome", self.verification_outcome),
            ("http_status", self.http_status),
            ("write_decision", self.write_decision),
            ("eligibility_reason", self.eligibility_reason),
            ("write_outcome", self.write_outcome),
        )
        result.update((key, value) for key, value in optional if value is not None)
        return result


def classify_reconciliation_warning(warning: str | None) -> str | None:
    """Return a stable allow-listed reason for known successful-but-degraded outcomes."""
    if warning is None:
        return None
    if warning.startswith(_SNAPSHOT_PROPAGATION_PREFIX):
        return "snapshot_propagation_pending"
    if warning.startswith("Spotify verification degraded: unavailable media item"):
        return "unavailable_media"
    return None


def classify_reconciliation_failure(error: str | None) -> ReconciliationFailureDiagnostic:
    """Classify only known shapes; unknown text collapses to a generic safe result."""
    if error is None:
        return ReconciliationFailureDiagnostic(
            "reconciliation_error",
            "reconciliation",
            failure_reason="unknown_reconciliation_error",
        )

    if error.startswith("desired state Spotify duration unavailable"):
        return ReconciliationFailureDiagnostic(
            "desired_state_error",
            "desired_state",
            failure_reason="desired_state_invalid",
            verification_outcome="failed",
            write_decision="blocked",
            eligibility_reason="duration_metadata_unavailable",
        )

    if error.startswith("Spotify prewrite mapping validation failed; destination preserved"):
        return ReconciliationFailureDiagnostic(
            "reconciliation_error",
            "prewrite",
            failure_reason="prewrite_mapping_validation_failed",
            verification_outcome="failed",
            write_decision="blocked",
        )

    if error.startswith(
        "Spotify prewrite desired-state preparation exceeded its bounded iterations"
    ):
        return ReconciliationFailureDiagnostic(
            "desired_state_error",
            "prewrite",
            failure_reason="prewrite_desired_iteration_limit",
            verification_outcome="failed",
            write_decision="blocked",
        )

    if error.startswith("Spotify authorization changed during cycle; destination preserved"):
        return ReconciliationFailureDiagnostic(
            "reconciliation_error",
            "authorization",
            failure_reason="authorization_context_changed",
            verification_outcome="failed",
            write_decision="preserved",
        )

    if error.startswith("destination preserved because local skip authority changed"):
        return ReconciliationFailureDiagnostic(
            "reconciliation_error",
            "verification",
            failure_reason="local_skip_authority_changed",
            verification_outcome="failed",
            write_decision="preserved",
        )

    operation_match = _OPERATION_FAILURE.match(error)
    if operation_match is not None:
        status = operation_match.group("status")
        operation = operation_match.group("operation")
        kind = operation_match.group("kind")
        return ReconciliationFailureDiagnostic(
            "api_error" if kind == "API" else "transport_error",
            operation_match.group("phase"),
            failure_reason=_operation_failure_reason(operation, kind),
            operation=operation,
            http_status=None if status is None else int(status),
            verification_outcome="failed",
            write_decision=(
                "blocked"
                if operation_match.group("phase") in {"prewrite", "verification"}
                else None
            ),
            write_outcome=operation_match.group("write_outcome"),
        )

    api_match = _API_ERROR.match(error)
    if api_match is not None:
        return ReconciliationFailureDiagnostic(
            "api_error",
            "reconciliation",
            failure_reason="unknown_reconciliation_error",
            http_status=int(api_match.group("status")),
            verification_outcome="failed",
        )
    if error == "Spotify API request failed due to a network error":
        return ReconciliationFailureDiagnostic(
            "transport_error",
            "reconciliation",
            failure_reason="unknown_reconciliation_error",
            verification_outcome="failed",
        )

    phase = _phase(error)
    values = {
        "offset": _number(_OFFSET, error),
        "returned_count": _number(_RETURNED, error),
        "total": _number(_TOTAL, error),
    }
    next_state = _next_state(error)

    if "pagination" in error:
        failure_class = "pagination_error"
        failure_reason = (
            "playlist_pagination_truncated"
            if "truncated before total" in error
            else "playlist_pagination_invalid"
        )
        verification = "failed"
        operation = "playlist_items"
    elif "snapshot" in error:
        failure_class = "snapshot_error"
        failure_reason = "snapshot_mismatch"
        verification = "mismatch" if "changed" in error else "failed"
        operation = "snapshot"
    elif "unavailable media" in error:
        failure_class = "unavailable_media"
        failure_reason = "unavailable_media"
        verification = "unavailable"
        operation = _operation(error)
    elif "did not match desired order/count/content" in error:
        failure_class = "verification_mismatch"
        failure_reason = "verification_mismatch"
        verification = "mismatch"
        operation = "playlist_items"
    elif _is_response_shape_failure(error):
        failure_class = "response_shape_error"
        failure_reason = "response_shape_invalid"
        verification = "failed"
        operation = "playlist_items"
    elif _is_configuration_failure(error):
        failure_class = "configuration_error"
        failure_reason = "configuration_invalid"
        verification = "failed"
        operation = _operation(error)
    elif "desired state" in error.casefold():
        failure_class = "desired_state_error"
        failure_reason = "desired_state_invalid"
        verification = "failed"
        operation = None
    else:
        failure_class = "reconciliation_error"
        failure_reason = "unknown_reconciliation_error"
        verification = "failed"
        operation = _operation(error)

    unavailable_count = _unavailable_count(error)
    write_decision = "blocked" if phase in {"prewrite", "verification"} else None
    return ReconciliationFailureDiagnostic(
        failure_class=failure_class,
        phase=phase,
        failure_reason=failure_reason,
        operation=operation,
        offset=values["offset"],
        returned_count=values["returned_count"],
        total=values["total"],
        next_state=next_state,
        unavailable_count=unavailable_count,
        verification_outcome=verification,
        write_decision=write_decision,
    )


def _operation_failure_reason(operation: str, kind: str) -> str:
    suffix = "api_error" if kind == "API" else "transport_error"
    return {
        "playlist_items": f"playlist_items_{suffix}",
        "replace_items": f"replace_items_{suffix}",
        "snapshot": f"snapshot_{suffix}",
    }[operation]


def _phase(error: str) -> str:
    lowered = error.casefold()
    if "authorization" in lowered:
        return "authorization"
    if "prewrite" in lowered:
        return "prewrite"
    if "readback" in lowered:
        return "readback"
    if "write response" in lowered:
        return "write"
    if "verification" in lowered:
        return "verification"
    if "desired state" in lowered:
        return "desired_state"
    return "reconciliation"


def _operation(error: str) -> str | None:
    lowered = error.casefold()
    if "playlist_items" in lowered or "playlist items" in lowered:
        return "playlist_items"
    if "replace_items" in lowered or "replace items" in lowered or "write response" in lowered:
        return "replace_items"
    if "snapshot" in lowered:
        return "snapshot"
    return None


def _next_state(error: str) -> str | None:
    lowered = error.casefold()
    if "missing next" in lowered:
        return "missing"
    if "invalid next" in lowered:
        return "invalid"
    if "next=null" in lowered:
        return "null"
    if "next=present" in lowered:
        return "present"
    return None


def _unavailable_count(error: str) -> int | None:
    match = _UNAVAILABLE_INDICES.search(error)
    if match is not None:
        values = [value for value in match.group("values").split(",") if value]
        return len(values)
    if "unavailable media item" in error:
        return 1
    return None


def _number(pattern: re.Pattern[str], error: str) -> int | None:
    match = pattern.search(error)
    return None if match is None else int(match.group("value"))


def _is_response_shape_failure(error: str) -> bool:
    lowered = error.casefold()
    return any(
        marker in lowered
        for marker in (
            "was not an object",
            "did not contain an item list",
            "invalid item",
            "without a media object",
            "contradictory media objects",
            "invalid media object",
            "without a uri",
        )
    )


def _is_configuration_failure(error: str) -> bool:
    lowered = error.casefold()
    return any(
        marker in lowered
        for marker in (
            " is disabled",
            "destination is not spotify",
            "does not belong to playlist",
            "snapshot reader is unavailable",
        )
    )
