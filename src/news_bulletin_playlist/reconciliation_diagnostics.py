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
    r"(?: \(http_status=(?P<status>\d{3})\))?$"
)
_OFFSET = re.compile(r"(?:^|[ (])offset=(?P<value>\d+)(?:[ )]|$)")
_RETURNED = re.compile(r"(?:^|[ (])returned=(?P<value>\d+)(?:[ )]|$)")
_TOTAL = re.compile(r"(?:^|[ (])total=(?P<value>\d+)(?:[ )]|$)")
_UNAVAILABLE_INDICES = re.compile(r"index/indices \[(?P<values>[0-9,]*)\]")


@dataclass(frozen=True, slots=True)
class ReconciliationFailureDiagnostic:
    """Safe classification that deliberately contains no provider/error text."""

    failure_class: str
    phase: str
    operation: str | None = None
    offset: int | None = None
    returned_count: int | None = None
    total: int | None = None
    next_state: str | None = None
    unavailable_count: int | None = None
    verification_outcome: str | None = None
    http_status: int | None = None
    write_decision: str | None = None

    def details(self) -> dict[str, DiagnosticValue]:
        result: dict[str, DiagnosticValue] = {
            "failure_class": self.failure_class,
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
        )
        result.update((key, value) for key, value in optional if value is not None)
        return result


def classify_reconciliation_failure(error: str | None) -> ReconciliationFailureDiagnostic:
    """Classify only known shapes; unknown text collapses to a generic safe result."""
    if error is None:
        return ReconciliationFailureDiagnostic("reconciliation_error", "reconciliation")

    operation_match = _OPERATION_FAILURE.match(error)
    if operation_match is not None:
        status = operation_match.group("status")
        return ReconciliationFailureDiagnostic(
            "api_error" if operation_match.group("kind") == "API" else "transport_error",
            operation_match.group("phase"),
            operation=operation_match.group("operation"),
            http_status=None if status is None else int(status),
            verification_outcome="failed",
            write_decision=(
                "blocked"
                if operation_match.group("phase") in {"prewrite", "verification"}
                else None
            ),
        )

    api_match = _API_ERROR.match(error)
    if api_match is not None:
        return ReconciliationFailureDiagnostic(
            "api_error",
            "reconciliation",
            http_status=int(api_match.group("status")),
            verification_outcome="failed",
        )
    if error == "Spotify API request failed due to a network error":
        return ReconciliationFailureDiagnostic(
            "transport_error",
            "reconciliation",
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
        verification = "failed"
    elif "snapshot" in error:
        failure_class = "snapshot_error"
        verification = "mismatch" if "changed" in error else "failed"
    elif "unavailable media" in error:
        failure_class = "unavailable_media"
        verification = "unavailable"
    elif "did not match desired order/count/content" in error:
        failure_class = "verification_mismatch"
        verification = "mismatch"
    elif _is_response_shape_failure(error):
        failure_class = "response_shape_error"
        verification = "failed"
    elif _is_configuration_failure(error):
        failure_class = "configuration_error"
        verification = "failed"
    elif "desired state" in error.casefold():
        failure_class = "desired_state_error"
        verification = "failed"
    else:
        failure_class = "reconciliation_error"
        verification = "failed"

    unavailable_count = _unavailable_count(error)
    write_decision = "blocked" if phase in {"prewrite", "verification"} else None
    return ReconciliationFailureDiagnostic(
        failure_class=failure_class,
        phase=phase,
        operation=_operation(error, phase),
        offset=values["offset"],
        returned_count=values["returned_count"],
        total=values["total"],
        next_state=next_state,
        unavailable_count=unavailable_count,
        verification_outcome=verification,
        write_decision=write_decision,
    )


def _phase(error: str) -> str:
    lowered = error.casefold()
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


def _operation(error: str, phase: str) -> str | None:
    lowered = error.casefold()
    if phase == "write" and "write response" in lowered:
        return "replace_items"
    if "snapshot" in lowered:
        return "snapshot"
    if phase in {"prewrite", "readback", "verification"}:
        return "playlist_items"
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
