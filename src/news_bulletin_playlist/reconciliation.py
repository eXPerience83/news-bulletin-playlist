from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Any, Protocol

from news_bulletin_playlist.desired_state import DesiredPlaylistState
from news_bulletin_playlist.models import PlaylistDefinition, PlaylistId
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.snapshot_pending import (
    PendingSnapshotConfirmation,
    PendingSnapshotJournal,
)
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError

_SPOTIFY_PLAYLIST_PAGE_SIZE = 50
_MANAGED_PLAYLIST_MAX_ITEMS = 100
_PENDING_SNAPSHOT_TTL = timedelta(hours=1)
_SNAPSHOT_PROPAGATION_WARNING = (
    "Spotify verification degraded: snapshot propagation pending after exact readback"
)
_PARTIAL_SNAPSHOT_PROPAGATION_WARNING = (
    "Spotify verification degraded: snapshot propagation pending after partial readback"
)


class SpotifyPlaylistClient(Protocol):
    """Small Spotify surface needed by playlist reconciliation."""

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]: ...

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]: ...

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]: ...


class SpotifyReconciliationError(RuntimeError):
    """Raised when Spotify playlist state cannot be read or verified safely."""


class MutationOutcome(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    VERIFIED = "verified"


@dataclass(slots=True)
class ReconciliationEvidence:
    """Observation only: survives exceptions without replacing their type or message."""

    mutation_outcome: MutationOutcome = MutationOutcome.NOT_ATTEMPTED
    remote_evidence_qualified: bool = False


PlaylistMutationGuard = Callable[[], AbstractContextManager[None]]
PlaylistPrewritePreparation = Callable[[DesiredPlaylistState], DesiredPlaylistState]


class _DesiredStateChanged(RuntimeError):
    def __init__(self, desired: DesiredPlaylistState) -> None:
        self.desired = desired


def _desired_preparation_key(
    desired: DesiredPlaylistState,
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (str(item.source_id), item.source_native_id, item.spotify_episode_uri)
        for item in desired.items
    )


@dataclass(frozen=True, slots=True)
class PlaylistReconciliationResult:
    playlist_id: PlaylistId
    ok: bool
    desired_count: int
    applied_count: int | None
    wrote: bool | None
    error: str | None = None
    degraded_verification: bool = False
    warning: str | None = None
    mutation_outcome: MutationOutcome = MutationOutcome.NOT_ATTEMPTED
    remote_evidence_qualified: bool = False
    verified_desired_uris: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class _PlaylistItemsReconciliationResult:
    wrote: bool
    degraded_verification: bool = False
    warning: str | None = None
    write_outcome: str = "verified"
    remote_evidence_qualified: bool = True


@dataclass(frozen=True, slots=True)
class _SpotifyPlaylistRead:
    slots: tuple[str | None, ...]

    @property
    def had_unavailable_item(self) -> bool:
        return any(slot is None for slot in self.slots)

    @property
    def unavailable_indices(self) -> tuple[int, ...]:
        return tuple(index for index, slot in enumerate(self.slots) if slot is None)


@dataclass(frozen=True, slots=True)
class _SpotifyPlaylistPage:
    items: list[object]
    next_url: str | None
    total: int


def _reconcile_playlist_items(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    desired_uris: Sequence[str],
    *,
    store: SQLiteStore | None,
    logical_playlist_id: PlaylistId | None,
    attestation_updated_at: datetime | None,
    mutation_guard: PlaylistMutationGuard | None,
    prepare_write: Callable[[], DesiredPlaylistState] | None = None,
    prewrite_desired: DesiredPlaylistState | None = None,
    snapshot_attested_prewrite: bool = False,
    evidence: ReconciliationEvidence | None = None,
) -> _PlaylistItemsReconciliationResult:
    evidence = evidence if evidence is not None else ReconciliationEvidence()
    desired = tuple(desired_uris)
    if len(desired) > _MANAGED_PLAYLIST_MAX_ITEMS:
        raise ValueError("playlist reconciliation is limited to 100 items")
    if (store is None) != (logical_playlist_id is None):
        raise ValueError("playlist attestation requires both store and logical playlist id")
    if store is not None and attestation_updated_at is None:
        raise ValueError("playlist attestation requires an update timestamp")

    desired_fingerprint = _desired_fingerprint(desired)
    pending_journal = PendingSnapshotJournal(store) if store is not None else None
    pending_confirmation: PendingSnapshotConfirmation | None = None
    pending_snapshot: str | None = None
    if (
        pending_journal is not None
        and store is not None
        and logical_playlist_id is not None
        and attestation_updated_at is not None
    ):
        pending_confirmation = pending_journal.get(logical_playlist_id)
        if pending_confirmation is not None:
            if (
                pending_confirmation.destination_id != playlist_id
                or pending_confirmation.desired_fingerprint != desired_fingerprint
                or attestation_updated_at >= pending_confirmation.expires_at
            ):
                pending_journal.clear(logical_playlist_id)
                pending_confirmation = None
            elif pending_confirmation.readback_exact:
                pending_snapshot = _read_current_snapshot(client, playlist_id, phase="prewrite")
                if pending_snapshot == pending_confirmation.expected_snapshot_id:
                    store.set_playlist_attestation(
                        logical_playlist_id,
                        destination_id=playlist_id,
                        snapshot_id=pending_confirmation.expected_snapshot_id,
                        desired_fingerprint=desired_fingerprint,
                        updated_at=attestation_updated_at,
                    )
                    pending_journal.clear(logical_playlist_id)
                    return _PlaylistItemsReconciliationResult(wrote=False)

    prewrite_snapshot: str | None = None
    current: _SpotifyPlaylistRead | None = None
    if (
        snapshot_attested_prewrite
        and pending_confirmation is None
        and store is not None
        and logical_playlist_id is not None
    ):
        prewrite_snapshot = _attested_prewrite_snapshot(
            client,
            playlist_id,
            desired_fingerprint=desired_fingerprint,
            store=store,
            logical_playlist_id=logical_playlist_id,
        )

    if prewrite_snapshot is None:
        current = _read_spotify_playlist(
            client,
            playlist_id,
            allow_unavailable=True,
            phase="prewrite",
        )

        if (
            pending_confirmation is not None
            and pending_journal is not None
            and store is not None
            and logical_playlist_id is not None
            and attestation_updated_at is not None
        ):
            pending_result = _resolve_pending_confirmation(
                client,
                playlist_id,
                desired,
                desired_fingerprint=desired_fingerprint,
                current=current,
                store=store,
                logical_playlist_id=logical_playlist_id,
                now=attestation_updated_at,
                journal=pending_journal,
                pending=pending_confirmation,
                current_snapshot=pending_snapshot,
            )
            if pending_result is not None:
                return pending_result

        if current.slots == desired:
            if len(current.slots) <= _SPOTIFY_PLAYLIST_PAGE_SIZE:
                return _PlaylistItemsReconciliationResult(wrote=False)
            snapshot_before = _read_current_snapshot(client, playlist_id, phase="prewrite")
            stable_current = _read_spotify_playlist(
                client,
                playlist_id,
                allow_unavailable=True,
                phase="prewrite",
            )
            snapshot_after = _read_current_snapshot(client, playlist_id, phase="prewrite")
            if snapshot_before == snapshot_after and stable_current.slots == desired:
                return _PlaylistItemsReconciliationResult(wrote=False)

        if (
            store is not None
            and logical_playlist_id is not None
            and current.had_unavailable_item
            and _visible_positions_match(current.slots, desired)
        ):
            attestation = store.get_playlist_attestation(logical_playlist_id)
            if (
                attestation is not None
                and attestation.destination_id == playlist_id
                and attestation.desired_fingerprint == desired_fingerprint
            ):
                # A confirmed attestation is a safety boundary. If its snapshot cannot be
                # checked, fail closed and preserve the destination; never reinterpret an
                # ambiguous read as permission to rewrite.
                prewrite_snapshot = _read_current_snapshot(
                    client,
                    playlist_id,
                    phase="prewrite",
                )
                if prewrite_snapshot == attestation.snapshot_id:
                    warning = _degraded_warning(current.unavailable_indices)
                    return _PlaylistItemsReconciliationResult(
                        wrote=False,
                        degraded_verification=True,
                        warning=warning,
                    )
            else:
                try:
                    prewrite_snapshot = _read_current_snapshot(
                        client,
                        playlist_id,
                        phase="prewrite",
                    )
                except SpotifyApiError, SpotifyTransportError, SpotifyReconciliationError:
                    # With no matching attestation this read is only a baseline for recognizing
                    # propagation after a subsequent write. The post-write path remains strict
                    # when the optional baseline is unavailable.
                    prewrite_snapshot = None

    if prepare_write is not None:
        prepared = prepare_write()
        if prewrite_desired is None:
            raise ValueError("prepare_write requires prewrite_desired")
        if prepared.items != prewrite_desired.items:
            raise _DesiredStateChanged(prepared)

    if store is not None and logical_playlist_id is not None and prewrite_snapshot is None:
        try:
            prewrite_snapshot = _read_current_snapshot(
                client,
                playlist_id,
                phase="prewrite",
            )
        except SpotifyApiError, SpotifyTransportError, SpotifyReconciliationError:
            # This read is only a baseline for recognizing later snapshot propagation. A failed
            # baseline must not block the write; the post-write path simply remains strict.
            prewrite_snapshot = None

    mutation_context = nullcontext() if mutation_guard is None else mutation_guard()
    with mutation_context:
        try:
            evidence.mutation_outcome = MutationOutcome.UNKNOWN
            write_response = client.replace_playlist_items(playlist_id, list(desired))
        except SpotifyApiError as exc:
            if exc.status < 500:
                evidence.mutation_outcome = MutationOutcome.REJECTED
            if exc.status >= 500 and _ambiguous_write_applied(client, playlist_id, desired):
                qualified = _attest_ambiguous_write(
                    client,
                    playlist_id,
                    desired=desired,
                    desired_fingerprint=desired_fingerprint,
                    store=store,
                    logical_playlist_id=logical_playlist_id,
                    updated_at=attestation_updated_at,
                    pending_journal=pending_journal,
                )
                return _PlaylistItemsReconciliationResult(
                    wrote=True,
                    write_outcome="verified",
                    remote_evidence_qualified=qualified,
                )
            raise SpotifyReconciliationError(
                f"Spotify playlist write replace_items API failure (http_status={exc.status})"
                + (" (write_outcome=unknown)" if exc.status >= 500 else "")
            ) from exc
        except SpotifyTransportError as exc:
            if _ambiguous_write_applied(client, playlist_id, desired):
                qualified = _attest_ambiguous_write(
                    client,
                    playlist_id,
                    desired=desired,
                    desired_fingerprint=desired_fingerprint,
                    store=store,
                    logical_playlist_id=logical_playlist_id,
                    updated_at=attestation_updated_at,
                    pending_journal=pending_journal,
                )
                return _PlaylistItemsReconciliationResult(
                    wrote=True,
                    write_outcome="verified",
                    remote_evidence_qualified=qualified,
                )
            raise SpotifyReconciliationError(
                "Spotify playlist write replace_items transport failure (write_outcome=unknown)"
            ) from exc

    allow_degraded_readback = store is not None and logical_playlist_id is not None
    readback = _read_spotify_playlist(
        client,
        playlist_id,
        allow_unavailable=allow_degraded_readback,
        phase="readback",
    )
    if not _visible_positions_match(readback.slots, desired):
        raise SpotifyReconciliationError(
            "Spotify playlist readback did not match desired order/count/content "
            f"(desired={len(desired)} returned={len(readback.slots)})"
        )

    if not readback.had_unavailable_item:
        write_snapshot = _optional_snapshot(write_response)
        pending_confirmation_after_write: PendingSnapshotConfirmation | None = None
        degraded_verification = False
        snapshot_warning: str | None = None
        if len(readback.slots) > _SPOTIFY_PLAYLIST_PAGE_SIZE or (
            store is not None and logical_playlist_id is not None
        ):
            write_snapshot = _require_snapshot(
                write_response,
                context="Spotify playlist write response",
            )
            if (
                prewrite_snapshot is not None
                and prewrite_snapshot != write_snapshot
                and pending_journal is not None
                and logical_playlist_id is not None
                and attestation_updated_at is not None
            ):
                pending_confirmation_after_write = PendingSnapshotConfirmation(
                    playlist_id=logical_playlist_id,
                    destination_id=playlist_id,
                    baseline_snapshot_id=prewrite_snapshot,
                    expected_snapshot_id=write_snapshot,
                    desired_fingerprint=desired_fingerprint,
                    created_at=attestation_updated_at,
                    expires_at=attestation_updated_at + _PENDING_SNAPSHOT_TTL,
                    readback_exact=True,
                )
                degraded_verification = True
                snapshot_warning = _SNAPSHOT_PROPAGATION_WARNING
                write_snapshot = None
            else:
                current_snapshot = _read_current_snapshot(
                    client,
                    playlist_id,
                    phase="readback",
                )
                if current_snapshot != write_snapshot:
                    if prewrite_snapshot is not None and current_snapshot == prewrite_snapshot:
                        if (
                            pending_journal is None
                            or logical_playlist_id is None
                            or attestation_updated_at is None
                        ):
                            raise SpotifyReconciliationError(
                                "Spotify playlist exact stale snapshot lacked durable "
                                "pending storage"
                            )
                        pending_confirmation_after_write = PendingSnapshotConfirmation(
                            playlist_id=logical_playlist_id,
                            destination_id=playlist_id,
                            baseline_snapshot_id=prewrite_snapshot,
                            expected_snapshot_id=write_snapshot,
                            desired_fingerprint=desired_fingerprint,
                            created_at=attestation_updated_at,
                            expires_at=attestation_updated_at + _PENDING_SNAPSHOT_TTL,
                            readback_exact=True,
                        )
                        degraded_verification = True
                        snapshot_warning = _SNAPSHOT_PROPAGATION_WARNING
                        write_snapshot = None
                    else:
                        # Without a known A -> B direct-pending transition, a third/unexpected
                        # snapshot still requires the stronger stability proof.
                        stable_readback = _read_spotify_playlist(
                            client,
                            playlist_id,
                            allow_unavailable=False,
                            phase="readback",
                        )
                        if stable_readback.slots != desired:
                            raise SpotifyReconciliationError(
                                "Spotify playlist snapshot changed because content changed "
                                "during exact snapshot recheck"
                            )
                        stable_snapshot = _read_current_snapshot(
                            client,
                            playlist_id,
                            phase="readback",
                        )
                        if stable_snapshot == write_snapshot:
                            pass
                        elif stable_snapshot == current_snapshot:
                            raise SpotifyReconciliationError(
                                "Spotify playlist snapshot did not match the known prewrite "
                                "baseline during exact readback verification"
                            )
                        else:
                            raise SpotifyReconciliationError(
                                "Spotify playlist snapshot advanced outside the expected write "
                                "transition"
                            )
        if store is not None and logical_playlist_id is not None:
            assert attestation_updated_at is not None
            if pending_confirmation_after_write is not None:
                assert pending_journal is not None
                pending_journal.set(pending_confirmation_after_write)
            else:
                if pending_journal is not None:
                    pending_journal.clear(logical_playlist_id)
                if write_snapshot is not None:
                    store.set_playlist_attestation(
                        logical_playlist_id,
                        destination_id=playlist_id,
                        snapshot_id=write_snapshot,
                        desired_fingerprint=desired_fingerprint,
                        updated_at=attestation_updated_at,
                    )
        return _PlaylistItemsReconciliationResult(
            wrote=True,
            degraded_verification=degraded_verification,
            warning=snapshot_warning,
        )

    if store is None or logical_playlist_id is None or attestation_updated_at is None:
        raise SpotifyReconciliationError(
            "Spotify playlist readback contained unavailable media without durable attestation"
        )

    write_snapshot = _require_snapshot(
        write_response,
        context="Spotify playlist write response",
    )
    current_snapshot = _read_current_snapshot(
        client,
        playlist_id,
        phase="readback",
    )
    if current_snapshot != write_snapshot:
        if prewrite_snapshot is not None and current_snapshot == prewrite_snapshot:
            assert pending_journal is not None
            pending_journal.set(
                PendingSnapshotConfirmation(
                    playlist_id=logical_playlist_id,
                    destination_id=playlist_id,
                    baseline_snapshot_id=prewrite_snapshot,
                    expected_snapshot_id=write_snapshot,
                    desired_fingerprint=desired_fingerprint,
                    created_at=attestation_updated_at,
                    expires_at=attestation_updated_at + _PENDING_SNAPSHOT_TTL,
                    readback_exact=False,
                )
            )
            return _PlaylistItemsReconciliationResult(
                wrote=True,
                degraded_verification=True,
                warning=_partial_snapshot_warning(readback.unavailable_indices),
            )
        raise SpotifyReconciliationError(
            "Spotify playlist snapshot changed during degraded readback verification"
        )

    store.set_playlist_attestation(
        logical_playlist_id,
        destination_id=playlist_id,
        snapshot_id=write_snapshot,
        desired_fingerprint=desired_fingerprint,
        updated_at=attestation_updated_at,
    )
    if pending_journal is not None:
        pending_journal.clear(logical_playlist_id)
    return _PlaylistItemsReconciliationResult(
        wrote=True,
        degraded_verification=True,
        warning=_degraded_warning(readback.unavailable_indices),
    )


def _ambiguous_write_applied(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    desired: Sequence[str],
) -> bool:
    """Resolve a mutating transport/5xx outcome only through a complete exact readback."""
    try:
        readback = _read_spotify_playlist(
            client,
            playlist_id,
            allow_unavailable=False,
            phase="readback",
        )
    except SpotifyApiError, SpotifyTransportError, SpotifyReconciliationError:
        return False
    return readback.slots == tuple(desired)


def _attest_ambiguous_write(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    *,
    desired: Sequence[str],
    desired_fingerprint: str,
    store: SQLiteStore | None,
    logical_playlist_id: PlaylistId | None,
    updated_at: datetime | None,
    pending_journal: PendingSnapshotJournal | None,
) -> bool:
    if store is None or logical_playlist_id is None or updated_at is None:
        return False
    try:
        snapshot_before = _read_current_snapshot(client, playlist_id, phase="readback")
        readback = _read_spotify_playlist(
            client,
            playlist_id,
            allow_unavailable=False,
            phase="readback",
        )
        snapshot_after = _read_current_snapshot(client, playlist_id, phase="readback")
    except SpotifyApiError, SpotifyTransportError, SpotifyReconciliationError:
        # The preceding complete exact readback already proves the ambiguous mutation applied.
        # Attestation is optional and must never convert that verified result into a retry.
        return False
    if readback.slots != tuple(desired) or snapshot_before != snapshot_after:
        return False
    try:
        store.set_playlist_attestation(
            logical_playlist_id,
            destination_id=playlist_id,
            snapshot_id=snapshot_after,
            desired_fingerprint=desired_fingerprint,
            updated_at=updated_at,
        )
        if pending_journal is not None:
            pending_journal.clear(logical_playlist_id)
    except PersistenceError:
        # The earlier exact readback still proves the mutation applied. Failure to persist
        # stronger skip evidence must not turn that provider result into a retry.
        return False
    return True


def _resolve_pending_confirmation(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    desired: Sequence[str],
    *,
    desired_fingerprint: str,
    current: _SpotifyPlaylistRead,
    store: SQLiteStore,
    logical_playlist_id: PlaylistId,
    now: datetime,
    journal: PendingSnapshotJournal,
    pending: PendingSnapshotConfirmation,
    current_snapshot: str | None = None,
) -> _PlaylistItemsReconciliationResult | None:
    if (
        pending.destination_id != playlist_id
        or pending.desired_fingerprint != desired_fingerprint
        or now >= pending.expires_at
    ):
        journal.clear(logical_playlist_id)
        return None

    if not _visible_positions_match(current.slots, desired):
        # Visible content has genuinely diverged. The old transition no longer authorizes a
        # skip; clear it and let normal reconciliation repair the destination.
        journal.clear(logical_playlist_id)
        return None

    # Exact pending state may have read the snapshot before item verification so the B fast path
    # can avoid item calls. Reuse that observation for A/C instead of issuing a duplicate read.
    if current_snapshot is None:
        current_snapshot = _read_current_snapshot(client, playlist_id, phase="prewrite")
    if current_snapshot == pending.expected_snapshot_id:
        store.set_playlist_attestation(
            logical_playlist_id,
            destination_id=playlist_id,
            snapshot_id=pending.expected_snapshot_id,
            desired_fingerprint=desired_fingerprint,
            updated_at=now,
        )
        journal.clear(logical_playlist_id)
        if current.had_unavailable_item:
            return _PlaylistItemsReconciliationResult(
                wrote=False,
                degraded_verification=True,
                warning=_degraded_warning(current.unavailable_indices),
            )
        return _PlaylistItemsReconciliationResult(wrote=False)

    if current_snapshot == pending.baseline_snapshot_id:
        warning = (
            _partial_snapshot_warning(current.unavailable_indices)
            if current.had_unavailable_item
            else _SNAPSHOT_PROPAGATION_WARNING
        )
        return _PlaylistItemsReconciliationResult(
            wrote=False,
            degraded_verification=True,
            warning=warning,
        )

    # Playlist metadata, cover and privacy changes can advance Spotify's playlist-wide
    # snapshot id without changing any item.  Accept that third snapshot only when a second
    # complete read proves the ordered URI state is still exact and the snapshot remains stable
    # across the recheck.  Partial reads intentionally retain the strict A/B boundary.
    if not current.had_unavailable_item and current.slots == tuple(desired):
        stable_current = _read_spotify_playlist(
            client,
            playlist_id,
            allow_unavailable=False,
            phase="prewrite",
        )
        stable_snapshot = _read_current_snapshot(client, playlist_id, phase="prewrite")
        if stable_current.slots == tuple(desired) and stable_snapshot == current_snapshot:
            store.set_playlist_attestation(
                logical_playlist_id,
                destination_id=playlist_id,
                snapshot_id=stable_snapshot,
                desired_fingerprint=desired_fingerprint,
                updated_at=now,
            )
            journal.clear(logical_playlist_id)
            return _PlaylistItemsReconciliationResult(wrote=False)

    raise SpotifyReconciliationError(
        "Spotify playlist snapshot advanced outside the pending confirmation transition"
    )


def reconcile_spotify_playlist(
    client: SpotifyPlaylistClient,
    playlist: PlaylistDefinition,
    desired: DesiredPlaylistState,
    *,
    store: SQLiteStore | None = None,
    mutation_guard: PlaylistMutationGuard | None = None,
    prepare_write: PlaylistPrewritePreparation | None = None,
    prewrite_iteration_limit: int | None = None,
    snapshot_attested_prewrite: bool = False,
    evidence: ReconciliationEvidence | None = None,
) -> PlaylistReconciliationResult:
    """Reconcile one Spotify destination; errors are left to the batch isolator."""
    if not playlist.enabled:
        raise SpotifyReconciliationError(f"playlist {playlist.id!s} is disabled")
    if str(playlist.destination.adapter_id) != "spotify":
        raise SpotifyReconciliationError(f"playlist {playlist.id!s} destination is not Spotify")
    if desired.playlist_id != playlist.id:
        raise SpotifyReconciliationError(
            f"desired state {desired.playlist_id!s} does not belong to playlist {playlist.id!s}"
        )

    evidence = evidence if evidence is not None else ReconciliationEvidence()
    current_desired = desired
    allow_snapshot_attested_prewrite = snapshot_attested_prewrite
    seen = {_desired_preparation_key(current_desired)}
    max_preparations = prewrite_iteration_limit or len(current_desired.items) + 1
    if max_preparations < 1:
        raise ValueError("prewrite_iteration_limit must be positive")
    for _ in range(max_preparations):
        prepared_callback = (
            None if prepare_write is None else partial(prepare_write, current_desired)
        )
        try:
            reconciled = _reconcile_playlist_items(
                client,
                playlist.destination.external_id,
                current_desired.uris,
                store=store,
                logical_playlist_id=playlist.id if store is not None else None,
                attestation_updated_at=(
                    current_desired.generated_at if store is not None else None
                ),
                mutation_guard=mutation_guard,
                prepare_write=prepared_callback,
                prewrite_desired=current_desired,
                snapshot_attested_prewrite=allow_snapshot_attested_prewrite,
                evidence=evidence,
            )
            break
        except _DesiredStateChanged as changed:
            if changed.desired.playlist_id != playlist.id:
                raise SpotifyReconciliationError(
                    "Spotify prewrite preparation returned desired state for another playlist"
                ) from None
            changed_key = _desired_preparation_key(changed.desired)
            if changed_key in seen:
                raise SpotifyReconciliationError(
                    "Spotify prewrite desired-state preparation made no monotonic progress"
                ) from None
            seen.add(changed_key)
            current_desired = changed.desired
            # The snapshot/attestation candidate was authorized for the original dirty desired
            # state. A #148 preparation change can make the rebuilt desired already equal the
            # remote playlist, so subsequent bounded iterations must restore the complete
            # pre-write item comparison instead of reusing the shortcut.
            allow_snapshot_attested_prewrite = False
    else:
        raise SpotifyReconciliationError(
            "Spotify prewrite desired-state preparation exceeded its bounded iterations"
        )
    evidence.remote_evidence_qualified = (
        reconciled.remote_evidence_qualified and not reconciled.degraded_verification
    )
    if reconciled.wrote:
        evidence.mutation_outcome = MutationOutcome.VERIFIED
    return PlaylistReconciliationResult(
        playlist_id=playlist.id,
        ok=True,
        desired_count=len(current_desired.items),
        applied_count=len(current_desired.items),
        wrote=reconciled.wrote,
        degraded_verification=reconciled.degraded_verification,
        warning=reconciled.warning,
        mutation_outcome=evidence.mutation_outcome,
        remote_evidence_qualified=evidence.remote_evidence_qualified,
        verified_desired_uris=(
            current_desired.uris if evidence.remote_evidence_qualified else None
        ),
    )


def reconcile_spotify_destinations(
    client: SpotifyPlaylistClient,
    plans: Sequence[tuple[PlaylistDefinition, DesiredPlaylistState]],
) -> tuple[PlaylistReconciliationResult, ...]:
    """Reconcile destinations independently so one Spotify failure cannot block another."""
    results: list[PlaylistReconciliationResult] = []
    for playlist, desired in plans:
        try:
            result = reconcile_spotify_playlist(client, playlist, desired)
        except (SpotifyApiError, SpotifyTransportError, SpotifyReconciliationError) as exc:
            result = PlaylistReconciliationResult(
                playlist_id=playlist.id,
                ok=False,
                desired_count=len(desired.items),
                applied_count=None,
                wrote=None,
                error=str(exc),
            )
        results.append(result)
    return tuple(results)


def _read_spotify_playlist(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    *,
    allow_unavailable: bool,
    phase: str,
) -> _SpotifyPlaylistRead:
    slots: list[str | None] = []
    offset = 0
    expected_total: int | None = None

    while offset < _MANAGED_PLAYLIST_MAX_ITEMS:
        limit = min(_SPOTIFY_PLAYLIST_PAGE_SIZE, _MANAGED_PLAYLIST_MAX_ITEMS - offset)
        raw_page = _read_playlist_page(
            client,
            playlist_id,
            limit=limit,
            offset=offset,
            phase=phase,
        )
        page = _require_playlist_page(raw_page, phase=phase, offset=offset)
        expected_total = _validate_total_consistency(
            page.total,
            expected_total=expected_total,
            phase=phase,
            offset=offset,
        )

        page_slots = _extract_playlist_slots(
            page.items,
            allow_unavailable=allow_unavailable,
            context=f"Spotify playlist {phase} response (offset={offset})",
        )
        slots.extend(page_slots)
        offset += len(page.items)

        if page.next_url is None:
            if len(page.items) == limit:
                overflow_slots = _read_overflow_item(
                    client,
                    playlist_id,
                    offset=offset,
                    allow_unavailable=allow_unavailable,
                    phase=phase,
                    expected_total=expected_total,
                )
                if overflow_slots:
                    slots.append(overflow_slots[0])
            return _SpotifyPlaylistRead(tuple(slots))

    overflow_slots = _read_overflow_item(
        client,
        playlist_id,
        offset=offset,
        allow_unavailable=allow_unavailable,
        phase=phase,
        expected_total=expected_total,
    )
    if overflow_slots:
        slots.append(overflow_slots[0])
    return _SpotifyPlaylistRead(tuple(slots))


def _read_playlist_page(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    *,
    limit: int,
    offset: int,
    phase: str,
) -> dict[str, Any]:
    try:
        return client.playlist_items(playlist_id, limit=limit, offset=offset)
    except SpotifyApiError as exc:
        raise SpotifyReconciliationError(
            f"Spotify playlist {phase} playlist_items API failure (http_status={exc.status})"
        ) from exc
    except SpotifyTransportError as exc:
        raise SpotifyReconciliationError(
            f"Spotify playlist {phase} playlist_items transport failure"
        ) from exc


def _read_overflow_item(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    *,
    offset: int,
    allow_unavailable: bool,
    phase: str,
    expected_total: int | None,
) -> list[str | None]:
    raw_overflow = _read_playlist_page(
        client,
        playlist_id,
        limit=1,
        offset=offset,
        phase=phase,
    )
    overflow = _require_playlist_page(raw_overflow, phase=phase, offset=offset)
    _validate_total_consistency(
        overflow.total,
        expected_total=expected_total,
        phase=phase,
        offset=offset,
    )
    if not overflow.items:
        return []
    return _extract_playlist_slots(
        overflow.items,
        allow_unavailable=allow_unavailable,
        context=f"Spotify playlist {phase} overflow response (offset={offset})",
    )


def _require_playlist_page(
    container: object,
    *,
    phase: str,
    offset: int,
) -> _SpotifyPlaylistPage:
    context = f"Spotify playlist {phase} response"
    if not isinstance(container, dict):
        raise SpotifyReconciliationError(f"{context} was not an object (offset={offset})")

    items = container.get("items")
    if not isinstance(items, list):
        raise SpotifyReconciliationError(
            f"{context} did not contain an item list (offset={offset})"
        )

    if "next" not in container:
        raise SpotifyReconciliationError(
            f"{context} pagination was missing next (offset={offset} returned={len(items)})"
        )
    next_value = container["next"]
    if next_value is not None and (not isinstance(next_value, str) or not next_value.strip()):
        raise SpotifyReconciliationError(
            f"{context} pagination contained invalid next (offset={offset} returned={len(items)})"
        )
    if not items and next_value is not None:
        raise SpotifyReconciliationError(
            f"{context} pagination advanced without returning an item (offset={offset})"
        )

    if "total" not in container:
        raise SpotifyReconciliationError(
            f"{context} pagination was missing total (offset={offset} returned={len(items)})"
        )
    total = container["total"]
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise SpotifyReconciliationError(
            f"{context} pagination contained invalid total (offset={offset} returned={len(items)})"
        )
    consumed = offset + len(items)
    if consumed > total:
        raise SpotifyReconciliationError(
            f"{context} pagination exceeded total "
            f"(offset={offset} returned={len(items)} total={total})"
        )
    if consumed < total and next_value is None:
        raise SpotifyReconciliationError(
            f"{context} pagination truncated before total "
            f"(offset={offset} returned={len(items)} total={total} next=null)"
        )
    if consumed >= total and next_value is not None:
        raise SpotifyReconciliationError(
            f"{context} pagination continued past total "
            f"(offset={offset} returned={len(items)} total={total} next=present)"
        )

    return _SpotifyPlaylistPage(items, next_value, total)


def _validate_total_consistency(
    total: int,
    *,
    expected_total: int | None,
    phase: str,
    offset: int,
) -> int:
    if expected_total is not None and total != expected_total:
        raise SpotifyReconciliationError(
            f"Spotify playlist {phase} pagination total changed "
            f"(offset={offset} expected_total={expected_total} total={total})"
        )
    return total


def _extract_playlist_slots(
    items: Sequence[object],
    *,
    allow_unavailable: bool,
    context: str,
) -> list[str | None]:
    slots: list[str | None] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise SpotifyReconciliationError(
                f"{context} contained an invalid item (item_index={index})"
            )
        has_item = "item" in item
        has_track = "track" in item
        if not has_item and not has_track:
            raise SpotifyReconciliationError(
                f"{context} contained an item without a media object (item_index={index})"
            )
        item_value = item.get("item") if has_item else None
        track_value = item.get("track") if has_track else None
        if item_value is not None and track_value is not None:
            item_uri = _require_media_uri(item_value, context=context, index=index)
            track_uri = _require_media_uri(track_value, context=context, index=index)
            if item_uri != track_uri:
                raise SpotifyReconciliationError(
                    f"{context} contained contradictory media objects (item_index={index})"
                )
            slots.append(item_uri)
            continue
        value = item_value if item_value is not None else track_value
        if value is None:
            if not allow_unavailable:
                raise SpotifyReconciliationError(
                    f"{context} contained an unavailable media item (item_index={index})"
                )
            slots.append(None)
            continue
        slots.append(_require_media_uri(value, context=context, index=index))
    return slots


def _require_media_uri(value: object, *, context: str, index: int) -> str:
    if not isinstance(value, dict):
        raise SpotifyReconciliationError(
            f"{context} contained an invalid media object (item_index={index})"
        )
    uri = value.get("uri")
    if not isinstance(uri, str) or not uri.strip():
        raise SpotifyReconciliationError(
            f"{context} contained an item without a URI (item_index={index})"
        )
    return uri


def _visible_positions_match(
    slots: Sequence[str | None],
    desired: Sequence[str],
) -> bool:
    if len(slots) != len(desired):
        return False
    return all(slot is None or slot == desired[index] for index, slot in enumerate(slots))


def _desired_fingerprint(desired: Sequence[str]) -> str:
    encoded = json.dumps(
        list(desired),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _attested_prewrite_snapshot(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    *,
    desired_fingerprint: str,
    store: SQLiteStore,
    logical_playlist_id: PlaylistId,
) -> str | None:
    """Return the settled attested baseline snapshot when it still matches remotely.

    This is an optimization hint only. It never qualifies the new desired state, publishes
    planner evidence or replaces post-write verification. Any missing, malformed, changed or
    unavailable evidence falls back to the normal complete pre-write item read.
    """
    try:
        attestation = store.get_playlist_attestation(logical_playlist_id)
    except PersistenceError:
        return None
    if (
        attestation is None
        or attestation.destination_id != playlist_id
        or not _is_sha256_fingerprint(attestation.desired_fingerprint)
        or attestation.desired_fingerprint == desired_fingerprint
        or not attestation.snapshot_id
        or attestation.snapshot_id != attestation.snapshot_id.strip()
    ):
        return None
    try:
        current_snapshot = _read_current_snapshot(client, playlist_id, phase="prewrite")
    except SpotifyApiError, SpotifyTransportError, SpotifyReconciliationError:
        return None
    return current_snapshot if current_snapshot == attestation.snapshot_id else None


def _is_sha256_fingerprint(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _read_current_snapshot(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    *,
    phase: str,
) -> str:
    snapshot_reader = getattr(client, "playlist_snapshot", None)
    if not callable(snapshot_reader):
        raise SpotifyReconciliationError(f"Spotify playlist {phase} snapshot reader is unavailable")
    response = snapshot_reader(playlist_id)
    return _require_snapshot(
        response,
        context=f"Spotify playlist {phase} snapshot response",
    )


def _require_snapshot(container: object, *, context: str) -> str:
    if not isinstance(container, dict):
        raise SpotifyReconciliationError(f"{context} was not an object")
    value = container.get("snapshot_id")
    if not isinstance(value, str) or not value.strip():
        raise SpotifyReconciliationError(f"{context} did not contain a valid snapshot_id")
    return value.strip()


def _optional_snapshot(container: object) -> str | None:
    try:
        return _require_snapshot(container, context="Spotify playlist write response")
    except SpotifyReconciliationError:
        return None


def _degraded_warning(indices: Sequence[int]) -> str:
    index_text = ",".join(str(index) for index in indices)
    return (
        "Spotify verification degraded: unavailable media item(s) at index/indices "
        f"[{index_text}]; snapshot attestation matched"
    )


def _partial_snapshot_warning(indices: Sequence[int]) -> str:
    index_text = ",".join(str(index) for index in indices)
    return (
        f"{_PARTIAL_SNAPSHOT_PROPAGATION_WARNING}; unavailable media item(s) at "
        f"index/indices [{index_text}]"
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)
