from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime

from news_bulletin_playlist.desired_state import DesiredPlaylistState
from news_bulletin_playlist.models import PlaylistDefinition
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.reconciliation import (
    MutationOutcome,
    PlaylistReconciliationResult,
    SpotifyPlaylistClient,
    SpotifyReconciliationError,
    _read_spotify_playlist,
)
from news_bulletin_playlist.snapshot_pending import PendingSnapshotJournal
from news_bulletin_playlist.soft_retention_context import soft_retention_audit_desired


def playlist_attestation_fingerprint(desired_uris: Sequence[str]) -> str:
    """Return the exact fingerprint format used by durable playlist attestations."""
    encoded = json.dumps(
        list(desired_uris),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def audit_spotify_playlist_snapshot_first(
    client: SpotifyPlaylistClient,
    playlist: PlaylistDefinition,
    desired: DesiredPlaylistState,
    *,
    store: SQLiteStore,
    observed_at: datetime,
) -> PlaylistReconciliationResult | None:
    """Verify one already-qualified periodic destination audit as cheaply as possible.

    ``None`` means that the snapshot shortcut could not prove the destination safe and the
    caller must continue through the existing conservative reconciliation path. Provider or
    malformed-response failures are intentionally propagated instead of being converted into a
    shortcut success.

    Snapshot equality is deliberately treated as an eventual-consistency version signal, not a
    linearizable proof of the current item state. Eligibility for calling this function is a
    separate planner responsibility; this function independently requires a settled attestation
    and no pending snapshot transition.

    When #181 has strongly proven an expiry-only transition, ``desired`` remains the strict
    logical 48-hour state while ``audit_desired`` is the still-authoritative physical baseline.
    Audits verify that baseline without turning preventive verification into a cleanup write.
    """
    if not playlist.enabled:
        raise SpotifyReconciliationError(f"playlist {playlist.id!s} is disabled")
    if str(playlist.destination.adapter_id) != "spotify":
        raise SpotifyReconciliationError(f"playlist {playlist.id!s} destination is not Spotify")
    if desired.playlist_id != playlist.id:
        raise SpotifyReconciliationError(
            f"desired state {desired.playlist_id!s} does not belong to playlist {playlist.id!s}"
        )

    pending_journal = PendingSnapshotJournal(store)
    if pending_journal.get(playlist.id) is not None:
        return None

    deferred_baseline = soft_retention_audit_desired(store, playlist, desired, observed_at)
    audit_desired = desired if deferred_baseline is None else deferred_baseline
    desired_fingerprint = playlist_attestation_fingerprint(audit_desired.uris)
    attestation = store.get_playlist_attestation(playlist.id)
    if (
        attestation is None
        or attestation.destination_id != playlist.destination.external_id
        or attestation.desired_fingerprint != desired_fingerprint
        or not attestation.snapshot_id
        or attestation.snapshot_id != attestation.snapshot_id.strip()
    ):
        return None

    attested_snapshot = attestation.snapshot_id
    current_snapshot = _read_snapshot(client, playlist.destination.external_id)
    if current_snapshot == attested_snapshot:
        return _qualified_result(playlist, desired, audit_desired)

    # A changed playlist-wide snapshot does not imply changed items. Bracket one complete read
    # with the already-observed S1 and a second S2 before rotating the strong attestation.
    current = _read_spotify_playlist(
        client,
        playlist.destination.external_id,
        allow_unavailable=True,
        phase="audit",
    )
    stable_snapshot = _read_snapshot(client, playlist.destination.external_id)
    if (
        stable_snapshot != current_snapshot
        or current.had_unavailable_item
        or current.slots != audit_desired.uris
    ):
        return None

    # Pending state is a stronger safety boundary than this optimization. Recheck it immediately
    # before publishing a new strong snapshot/content binding.
    if pending_journal.get(playlist.id) is not None:
        return None

    if deferred_baseline is None:
        store.set_playlist_attestation(
            playlist.id,
            destination_id=playlist.destination.external_id,
            snapshot_id=stable_snapshot,
            desired_fingerprint=desired_fingerprint,
            updated_at=observed_at,
        )
    # During soft-retention grace, keep the original attestation timestamp/fingerprint as the
    # semantic physical baseline. A metadata-only snapshot advance is still qualifying remote
    # evidence for the planner, but must not make old items appear newly attested at this time.
    return _qualified_result(playlist, desired, audit_desired)


def _read_snapshot(client: SpotifyPlaylistClient, playlist_id: str) -> str:
    response = client.playlist_snapshot(playlist_id)
    if not isinstance(response, dict):
        raise SpotifyReconciliationError(
            "Spotify playlist audit snapshot response was not an object"
        )
    value = response.get("snapshot_id")
    if not isinstance(value, str) or not value.strip():
        raise SpotifyReconciliationError(
            "Spotify playlist audit snapshot response did not contain a valid snapshot_id"
        )
    return value.strip()


def _qualified_result(
    playlist: PlaylistDefinition,
    logical_desired: DesiredPlaylistState,
    verified_desired: DesiredPlaylistState,
) -> PlaylistReconciliationResult:
    return PlaylistReconciliationResult(
        playlist_id=playlist.id,
        ok=True,
        desired_count=len(logical_desired.items),
        applied_count=len(verified_desired.items),
        wrote=False,
        mutation_outcome=MutationOutcome.NOT_ATTEMPTED,
        remote_evidence_qualified=True,
        verified_desired_uris=verified_desired.uris,
    )
