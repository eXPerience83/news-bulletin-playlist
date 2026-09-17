"""Cycle-local bridge for bounded soft-retention policy diagnostics."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.soft_retention import SoftRetentionDecision, SoftRetentionDisposition


@dataclass(frozen=True, slots=True)
class SoftRetentionOutcomeDiagnostic:
    playlist_id: PlaylistId
    disposition: SoftRetentionDisposition
    soft_expired_count: int
    oldest_soft_expired_age_seconds: int


SoftRetentionDiagnosticSink = Callable[[SoftRetentionOutcomeDiagnostic], None]

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
