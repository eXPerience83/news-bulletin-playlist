"""Cycle-local bridge between soft retention planning and destination audits."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

from news_bulletin_playlist.desired_state import DesiredPlaylistState
from news_bulletin_playlist.models import PlaylistDefinition
from news_bulletin_playlist.persistence import SQLiteStore

SoftRetentionAuditResolver = Callable[
    [SQLiteStore, PlaylistDefinition, DesiredPlaylistState, datetime],
    DesiredPlaylistState | None,
]

_AUDIT_RESOLVER: ContextVar[SoftRetentionAuditResolver | None] = ContextVar(
    "soft_retention_audit_resolver",
    default=None,
)


@contextmanager
def bind_soft_retention_audit_resolver(
    resolver: SoftRetentionAuditResolver,
) -> Iterator[None]:
    """Bind one engine runner's resolver for the duration of its serialized cycle."""
    token = _AUDIT_RESOLVER.set(resolver)
    try:
        yield
    finally:
        _AUDIT_RESOLVER.reset(token)


def soft_retention_audit_desired(
    store: SQLiteStore,
    playlist: PlaylistDefinition,
    logical_desired: DesiredPlaylistState,
    observed_at: datetime,
) -> DesiredPlaylistState | None:
    """Return the proven physical baseline when a cycle is deferring expiry-only cleanup."""
    resolver = _AUDIT_RESOLVER.get()
    if resolver is None:
        return None
    return resolver(store, playlist, logical_desired, observed_at)
