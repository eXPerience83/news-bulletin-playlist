"""Cycle-local bridge for bounded soft-retention policy diagnostics."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.soft_retention import SoftRetentionDecision, SoftRetentionDisposition


class SoftRetentionRejectionReason(StrEnum):
    """Bounded reasons why existing evidence cannot authorize soft retention."""

    PLAYLIST_MISMATCH = "playlist_mismatch"
    PENDING_SNAPSHOT = "pending_snapshot"
    MISSING_ATTESTATION = "missing_attestation"
    DESTINATION_MISMATCH = "destination_mismatch"
    MISSING_DESTINATION_STATE = "missing_destination_state"
    DIRTY_DESTINATION = "dirty_destination"
    UNKNOWN_DESTINATION = "unknown_destination"
    MISSING_DESTINATION_CONFIRMATION = "missing_destination_confirmation"
    DESIRED_FINGERPRINT_MISMATCH = "desired_fingerprint_mismatch"
    MISSING_DESTINATION_VERIFICATION = "missing_destination_verification"
    CLOCK_ROLLBACK = "clock_rollback"
    CONFIG_MISMATCH = "config_mismatch"
    AUTH_MISMATCH = "auth_mismatch"
    MISSING_SOURCE = "missing_source"
    ATTESTATION_MISMATCH = "attestation_mismatch"
    MATCHING_EDITION_MISSING = "matching_edition_missing"
    MATCHING_STATE_MISSING = "matching_state_missing"
    MATCHING_PENDING = "matching_pending"
    MATCHING_AMBIGUOUS = "matching_ambiguous"
    MATCHING_MAPPING_CHANGED = "matching_mapping_changed"
    MATCHING_EVIDENCE_CHANGED = "matching_evidence_changed"
    MATCHING_PERSISTENCE_REJECTION_BARRIER = "matching_persistence_rejection_barrier"
    MATCHING_RECOVERY_BACKOFF = "matching_recovery_backoff"
    MATCHING_RECOVERY_DUE = "matching_recovery_due"
    MATCHING_INPUT_MISSING = "matching_input_missing"
    MATCHING_INPUT_CHANGED = "matching_input_changed"
    NOT_EXPIRY_ONLY = "not_expiry_only"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"


@dataclass(frozen=True, slots=True)
class SoftRetentionOutcomeDiagnostic:
    playlist_id: PlaylistId
    disposition: SoftRetentionDisposition
    soft_expired_count: int
    oldest_soft_expired_age_seconds: int


@dataclass(frozen=True, slots=True)
class SoftRetentionRejectionDiagnostic:
    playlist_id: PlaylistId
    reason: SoftRetentionRejectionReason


type SoftRetentionDiagnostic = SoftRetentionOutcomeDiagnostic | SoftRetentionRejectionDiagnostic
SoftRetentionDiagnosticSink = Callable[[SoftRetentionDiagnostic], None]

_DIAGNOSTIC_SINK: ContextVar[SoftRetentionDiagnosticSink | None] = ContextVar(
    "soft_retention_diagnostic_sink",
    default=None,
)


@contextmanager
def bind_soft_retention_diagnostic_sink(
    sink: SoftRetentionDiagnosticSink,
) -> Iterator[None]:
    """Bind one production observer for the duration of an engine cycle."""
    token = _DIAGNOSTIC_SINK.set(sink)
    try:
        yield
    finally:
        _DIAGNOSTIC_SINK.reset(token)


def publish_soft_retention_decision(
    playlist_id: PlaylistId,
    decision: SoftRetentionDecision,
) -> None:
    """Publish only positive expiry-only classifications; never provider values."""
    if not decision.expiry_only or decision.oldest_soft_expired_age_seconds is None:
        return
    sink = _DIAGNOSTIC_SINK.get()
    if sink is None:
        return
    sink(
        SoftRetentionOutcomeDiagnostic(
            playlist_id=playlist_id,
            disposition=decision.disposition,
            soft_expired_count=decision.soft_expired_count,
            oldest_soft_expired_age_seconds=decision.oldest_soft_expired_age_seconds,
        )
    )


def publish_soft_retention_rejection(
    playlist_id: PlaylistId,
    reason: SoftRetentionRejectionReason,
) -> None:
    """Publish a bounded evidence rejection without exposing persistence/provider values."""
    if reason is SoftRetentionRejectionReason.NOT_EXPIRY_ONLY:
        return
    sink = _DIAGNOSTIC_SINK.get()
    if sink is None:
        return
    sink(SoftRetentionRejectionDiagnostic(playlist_id=playlist_id, reason=reason))
