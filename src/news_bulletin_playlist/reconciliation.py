from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from news_bulletin_playlist.desired_state import (
    DesiredPlaylistState,
    authoritative_playlist_time,
    build_playlist_desired_state,
)
from news_bulletin_playlist.models import OrderingPolicy, PlaylistDefinition, PlaylistId, SourceId
from news_bulletin_playlist.persistence import EditionMatch, SQLiteStore
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError

_SPOTIFY_PLAYLIST_PAGE_SIZE = 50
_MANAGED_PLAYLIST_MAX_ITEMS = 100
_SNAPSHOT_PROPAGATION_WARNING = (
    "Spotify verification degraded: snapshot propagation pending after exact readback"
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


@dataclass(frozen=True, slots=True)
class _PlaylistItemsReconciliationResult:
    wrote: bool
    degraded_verification: bool = False
    warning: str | None = None


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


def build_desired_state_from_store(
    store: SQLiteStore,
    playlist: PlaylistDefinition,
    *,
    now: datetime,
) -> DesiredPlaylistState:
    """Load only still-relevant durable state, then delegate to the pure builder."""
    observed_at = _as_utc(now)
    cutoff = observed_at - timedelta(hours=playlist.retention_hours)
    editions = []
    matches: dict[tuple[SourceId, str], EditionMatch] = {}

    for source_id in playlist.source_selection.explicit:
        for edition in store.list_editions(source_id=source_id):
            ordering_at = authoritative_playlist_time(edition, playlist.ordering)
            if playlist.ordering is OrderingPolicy.PUBLISHED_AT_DESC and ordering_at < cutoff:
                break
            if ordering_at < cutoff or ordering_at > observed_at:
                continue
            editions.append(edition)
            match = store.get_match_state(edition.source_id, edition.source_native_id)
            if match is not None:
                matches[edition.identity] = match

    return build_playlist_desired_state(playlist, editions, matches, now=observed_at)


def read_spotify_playlist_uris(
    client: SpotifyPlaylistClient,
    playlist_id: str,
) -> tuple[str, ...]:
    """Read the complete bounded playlist and fail closed on unavailable media."""
    read = _read_spotify_playlist(
        client,
        playlist_id,
        allow_unavailable=False,
        phase="verification",
    )
    return tuple(slot for slot in read.slots if slot is not None)


def reconcile_playlist_items(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    desired_uris: Sequence[str],
) -> bool:
    """Legacy strict helper: replace only on change and require exact readable readback."""
    return _reconcile_playlist_items(
        client,
        playlist_id,
        desired_uris,
        store=None,
        logical_playlist_id=None,
        attestation_updated_at=None,
    ).wrote


def _reconcile_playlist_items(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    desired_uris: Sequence[str],
    *,
    store: SQLiteStore | None,
    logical_playlist_id: PlaylistId | None,
    attestation_updated_at: datetime | None,
) -> _PlaylistItemsReconciliationResult:
    desired = tuple(desired_uris)
    if len(desired) > _MANAGED_PLAYLIST_MAX_ITEMS:
        raise ValueError("playlist reconciliation is limited to 100 items")
    if (store is None) != (logical_playlist_id is None):
        raise ValueError("playlist attestation requires both store and logical playlist id")
    if store is not None and attestation_updated_at is None:
        raise ValueError("playlist attestation requires an update timestamp")

    current = _read_spotify_playlist(
        client,
        playlist_id,
        allow_unavailable=True,
        phase="prewrite",
    )
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

    desired_fingerprint = _desired_fingerprint(desired)
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
            try:
                current_snapshot = _read_current_snapshot(
                    client,
                    playlist_id,
                    phase="prewrite",
                )
            except SpotifyReconciliationError:
                current_snapshot = None
            if current_snapshot == attestation.snapshot_id:
                warning = _degraded_warning(current.unavailable_indices)
                return _PlaylistItemsReconciliationResult(
                    wrote=False,
                    degraded_verification=True,
                    warning=warning,
                )

    try:
        write_response = client.replace_playlist_items(playlist_id, list(desired))
    except SpotifyApiError as exc:
        raise SpotifyReconciliationError(
            f"Spotify playlist write replace_items API failure (http_status={exc.status})"
        ) from exc
    except SpotifyTransportError as exc:
        raise SpotifyReconciliationError(
            "Spotify playlist write replace_items transport failure"
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
        degraded_verification = False
        snapshot_warning: str | None = None
        if len(readback.slots) > _SPOTIFY_PLAYLIST_PAGE_SIZE:
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
                stable_readback = _read_spotify_playlist(
                    client,
                    playlist_id,
                    allow_unavailable=False,
                    phase="readback",
                )
                if stable_readback.slots != desired:
                    raise SpotifyReconciliationError(
                        "Spotify playlist content changed during exact snapshot recheck"
                    )
                stable_snapshot = _read_current_snapshot(
                    client,
                    playlist_id,
                    phase="readback",
                )
                if stable_snapshot == write_snapshot:
                    pass
                elif stable_snapshot == current_snapshot:
                    degraded_verification = True
                    snapshot_warning = _SNAPSHOT_PROPAGATION_WARNING
                    write_snapshot = None
                else:
                    raise SpotifyReconciliationError(
                        "Spotify playlist snapshot remained unstable during exact "
                        "readback verification"
                    )
        if store is not None and logical_playlist_id is not None:
            assert attestation_updated_at is not None
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
    return _PlaylistItemsReconciliationResult(
        wrote=True,
        degraded_verification=True,
        warning=_degraded_warning(readback.unavailable_indices),
    )


def reconcile_spotify_playlist(
    client: SpotifyPlaylistClient,
    playlist: PlaylistDefinition,
    desired: DesiredPlaylistState,
    *,
    store: SQLiteStore | None = None,
) -> PlaylistReconciliationResult:
    """Reconcile one Spotify destination; errors are left to the batch isolator."""
    if not playlist.enabled:
        raise SpotifyReconciliationError(f"playlist {playlist.id!s} is disabled")
    if str(playlist.destination.adapter_id) != "spotify":
        raise SpotifyReconciliationError(
            f"playlist {playlist.id!s} destination is not Spotify"
        )
    if desired.playlist_id != playlist.id:
        raise SpotifyReconciliationError(
            f"desired state {desired.playlist_id!s} does not belong to playlist {playlist.id!s}"
        )

    reconciled = _reconcile_playlist_items(
        client,
        playlist.destination.external_id,
        desired.uris,
        store=store,
        logical_playlist_id=playlist.id if store is not None else None,
        attestation_updated_at=desired.generated_at if store is not None else None,
    )
    return PlaylistReconciliationResult(
        playlist_id=playlist.id,
        ok=True,
        desired_count=len(desired.items),
        applied_count=len(desired.items),
        wrote=reconciled.wrote,
        degraded_verification=reconciled.degraded_verification,
        warning=reconciled.warning,
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
            f"{context} pagination was missing next "
            f"(offset={offset} returned={len(items)})"
        )
    next_value = container["next"]
    if next_value is not None and (
        not isinstance(next_value, str) or not next_value.strip()
    ):
        raise SpotifyReconciliationError(
            f"{context} pagination contained invalid next "
            f"(offset={offset} returned={len(items)})"
        )
    if not items and next_value is not None:
        raise SpotifyReconciliationError(
            f"{context} pagination advanced without returning an item (offset={offset})"
        )

    if "total" not in container:
        raise SpotifyReconciliationError(
            f"{context} pagination was missing total "
            f"(offset={offset} returned={len(items)})"
        )
    total = container["total"]
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise SpotifyReconciliationError(
            f"{context} pagination contained invalid total "
            f"(offset={offset} returned={len(items)})"
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
                f"{context} contained an item without a media object "
                f"(item_index={index})"
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
                    f"{context} contained an unavailable media item "
                    f"(item_index={index})"
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


def _read_current_snapshot(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    *,
    phase: str,
) -> str:
    snapshot_reader = getattr(client, "playlist_snapshot", None)
    if not callable(snapshot_reader):
        raise SpotifyReconciliationError(
            f"Spotify playlist {phase} snapshot reader is unavailable"
        )
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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)
