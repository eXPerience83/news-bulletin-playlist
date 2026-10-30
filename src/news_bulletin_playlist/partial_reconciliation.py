"""Non-destructive insertion path for fresh, positively observed partial catalogues.

A null in a show catalogue never licenses a full playlist replacement. This module
only inserts known episode URIs into an exactly readable Spotify destination.
An on-disk intent fences uncertain POST outcomes and process restarts.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.reconciliation import (
    SpotifyPlaylistClient,
    SpotifyReconciliationError,
    _read_current_snapshot,
    _read_spotify_playlist,
)
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError
from news_bulletin_playlist.spotify.uris import is_spotify_episode_uri

_MAX_ITEMS = 100
_INTENT_TABLE = """
CREATE TABLE IF NOT EXISTS spotify_partial_add_intent (
    playlist_id TEXT PRIMARY KEY,
    destination_id TEXT NOT NULL,
    expected_uris_json TEXT NOT NULL
)
"""


class PartialAdditionClient(SpotifyPlaylistClient, Protocol):
    """The existing reconciler client includes the guarded POST operation."""


type PartialAddStatus = Literal["added", "unchanged", "capacity", "pending"]


@dataclass(frozen=True, slots=True)
class PartialAddResult:
    status: PartialAddStatus
    added_count: int = 0
    remote_count: int | None = None


@dataclass(frozen=True, slots=True)
class _PendingAddition:
    destination_id: str
    expected_uris: tuple[str, ...]


class _PartialAddIntentStore:
    """A persistent one-playlist fence; never retry an unverified mutation."""

    def __init__(self, store: SQLiteStore) -> None:
        self.path = store.path

    def _connection(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path)
            connection.execute(_INTENT_TABLE)
            return connection
        except sqlite3.Error as exc:
            raise PersistenceError("partial addition journal unavailable") from exc

    def get(self, playlist_id: PlaylistId) -> _PendingAddition | None:
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT destination_id, expected_uris_json FROM spotify_partial_add_intent "
                "WHERE playlist_id = ?",
                (str(playlist_id),),
            ).fetchone()
            if row is None:
                return None
            raw = json.loads(row[1])
            if (
                not isinstance(raw, list)
                or not raw
                or len(raw) > _MAX_ITEMS
                or any(not isinstance(uri, str) or not is_spotify_episode_uri(uri) for uri in raw)
            ):
                raise PersistenceError("partial addition journal contains invalid expected content")
            return _PendingAddition(row[0], tuple(raw))
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise PersistenceError("partial addition journal unreadable") from exc
        finally:
            connection.close()

    def begin(
        self, playlist_id: PlaylistId, destination_id: str, expected: tuple[str, ...]
    ) -> None:
        connection = self._connection()
        try:
            with connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO spotify_partial_add_intent "
                    "(playlist_id, destination_id, expected_uris_json) VALUES (?, ?, ?)",
                    (str(playlist_id), destination_id, json.dumps(expected)),
                )
                if cursor.rowcount != 1:
                    raise PersistenceError("partial addition already pending")
        except sqlite3.Error as exc:
            raise PersistenceError("partial addition journal write failed") from exc
        finally:
            connection.close()

    def clear(self, playlist_id: PlaylistId, expected: tuple[str, ...]) -> None:
        connection = self._connection()
        try:
            with connection:
                connection.execute(
                    "DELETE FROM spotify_partial_add_intent "
                    "WHERE playlist_id = ? AND expected_uris_json = ?",
                    (str(playlist_id), json.dumps(expected)),
                )
        except sqlite3.Error as exc:
            raise PersistenceError("partial addition journal clear failed") from exc
        finally:
            connection.close()


def clear_partial_add_intent(store: SQLiteStore, playlist_id: PlaylistId) -> None:
    """Clear only after a fully authoritative normal reconciliation succeeded."""
    journal = _PartialAddIntentStore(store)
    connection = journal._connection()
    try:
        with connection:
            connection.execute(
                "DELETE FROM spotify_partial_add_intent WHERE playlist_id = ?",
                (str(playlist_id),),
            )
    except sqlite3.Error as exc:
        raise PersistenceError("partial addition journal clear failed") from exc
    finally:
        connection.close()


def _stable_destination(
    client: PartialAdditionClient, destination_id: str, *, phase: str
) -> tuple[str, tuple[str, ...]]:
    before = _read_current_snapshot(client, destination_id, phase=phase)
    current = _read_spotify_playlist(client, destination_id, allow_unavailable=False, phase=phase)
    after = _read_current_snapshot(client, destination_id, phase=phase)
    if before != after:
        raise SpotifyReconciliationError(
            "partial addition destination snapshot changed during verification"
        )
    if any(uri is None for uri in current.slots):
        raise SpotifyReconciliationError("partial addition destination contains unknown media")
    return after, tuple(uri for uri in current.slots if uri is not None)


def _confirm_expected(
    client: PartialAdditionClient, destination_id: str, expected: tuple[str, ...]
) -> bool:
    try:
        _snapshot, observed = _stable_destination(client, destination_id, phase="readback")
        return observed == expected
    except SpotifyApiError, SpotifyTransportError, SpotifyReconciliationError:
        return False


def reconcile_partial_additions(
    client: PartialAdditionClient,
    *,
    playlist_id: PlaylistId,
    destination_id: str,
    candidate_uris: Sequence[str],
    max_items: int,
    store: SQLiteStore,
    mutation_guard: Callable[[], AbstractContextManager[None]],
) -> PartialAddResult:
    """Append-only recovery, with exact prewrite/readback and a durable retry fence.

    Failing to read a complete destination, changed snapshots, 429, unknown POST
    outcome or a capacity shortfall never triggers a replacement or a retry.
    """
    if not 1 <= max_items <= _MAX_ITEMS:
        raise ValueError("partial addition max_items must be in 1..100")
    if len(set(candidate_uris)) != len(candidate_uris):
        raise ValueError("partial addition candidates contain duplicates")
    if any(not is_spotify_episode_uri(uri) for uri in candidate_uris):
        raise ValueError("partial addition candidates contain invalid episode URIs")
    if not candidate_uris:
        return PartialAddResult("unchanged")

    journal = _PartialAddIntentStore(store)
    with mutation_guard():
        pending = journal.get(playlist_id)
        if pending is not None:
            if pending.destination_id != destination_id:
                return PartialAddResult("pending")
            if not _confirm_expected(client, destination_id, pending.expected_uris):
                return PartialAddResult("pending")
            journal.clear(playlist_id, pending.expected_uris)
            # Deliberately stop after resolving an old POST; never chain inserts.
            return PartialAddResult("unchanged", remote_count=len(pending.expected_uris))

        _snapshot, current = _stable_destination(client, destination_id, phase="prewrite")
        if len(current) > max_items:
            return PartialAddResult("capacity", remote_count=len(current))
        existing = set(current)
        additions = tuple(uri for uri in candidate_uris if uri not in existing)
        if not additions:
            return PartialAddResult("unchanged", remote_count=len(current))
        if len(current) + len(additions) > max_items:
            return PartialAddResult("capacity", remote_count=len(current))

        expected = additions + current
        journal.begin(playlist_id, destination_id, expected)
        try:
            response = client.add_playlist_items(destination_id, list(additions), position=0)
        except SpotifyApiError as exc:
            if exc.status < 500:
                journal.clear(playlist_id, expected)
            elif _confirm_expected(client, destination_id, expected):
                journal.clear(playlist_id, expected)
                return PartialAddResult("added", len(additions), len(expected))
            raise
        except SpotifyTransportError:
            if _confirm_expected(client, destination_id, expected):
                journal.clear(playlist_id, expected)
                return PartialAddResult("added", len(additions), len(expected))
            raise

        if not isinstance(response, dict) or not isinstance(response.get("snapshot_id"), str):
            raise SpotifyReconciliationError(
                "partial addition POST lacked a valid snapshot; intent retained"
            )
        post_snapshot, observed = _stable_destination(client, destination_id, phase="readback")
        if observed != expected or post_snapshot != response["snapshot_id"]:
            raise SpotifyReconciliationError(
                "partial addition readback/snapshot was not exact; intent retained"
            )
        journal.clear(playlist_id, expected)
        return PartialAddResult("added", len(additions), len(expected))
