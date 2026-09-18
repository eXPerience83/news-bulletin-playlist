from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from test_shadow_planner_review import setup

from news_bulletin_playlist.desired_state import DesiredPlaylistItem, DesiredPlaylistState
from news_bulletin_playlist.models import (
    AdapterId,
    CountryCode,
    DestinationReference,
    LanguageTag,
    PlaylistDefinition,
    PlaylistId,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.reconciliation import reconcile_spotify_playlist
from news_bulletin_playlist.snapshot_pending import (
    PendingSnapshotConfirmation,
    PendingSnapshotJournal,
)
from news_bulletin_playlist.spotify.client import SpotifyTransportError
from news_bulletin_playlist.spotify_destination_audit import playlist_attestation_fingerprint
from news_bulletin_playlist.spotify_planner import DestinationVerificationContext

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId("spain_spanish_news"),
        display_name="Noticias España",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SourceId("ser"),)),
        destination=DestinationReference(AdapterId("spotify"), "playlist"),
    )


def _desired(playlist: PlaylistDefinition, count: int, *, suffix: str = "new") -> DesiredPlaylistState:
    return DesiredPlaylistState(
        playlist_id=playlist.id,
        generated_at=NOW,
        items=tuple(
            DesiredPlaylistItem(
                source_id=SourceId("ser"),
                source_native_id=f"{suffix}-{index}",
                published_at=NOW - timedelta(minutes=index),
                spotify_episode_uri=f"spotify:episode:{suffix}-{index}",
            )
            for index in range(count)
        ),
    )


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def _seed_attestation(
    store: SQLiteStore,
    playlist: PlaylistDefinition,
    uris: tuple[str, ...],
    *,
    snapshot: str = "snapshot-a",
) -> None:
    store.set_playlist_attestation(
        playlist.id,
        destination_id=playlist.destination.external_id,
        snapshot_id=snapshot,
        desired_fingerprint=playlist_attestation_fingerprint(uris),
        updated_at=NOW - timedelta(minutes=10),
    )


class _Spotify:
    def __init__(self, uris: tuple[str, ...], snapshots: list[str | Exception]) -> None:
        self.items = list(uris)
        self.snapshots = list(snapshots)
        self.item_calls: list[tuple[int, int]] = []
        self.snapshot_calls = 0
        self.writes = 0

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del playlist_id
        self.item_calls.append((limit, offset))
        page = self.items[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in page],
            "next": "next" if offset + len(page) < len(self.items) else None,
            "total": len(self.items),
        }

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        del playlist_id
        index = min(self.snapshot_calls, len(self.snapshots) - 1)
        value = self.snapshots[index]
        self.snapshot_calls += 1
        if isinstance(value, Exception):
            raise value
        return {"snapshot_id": value}

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        del playlist_id
        self.writes += 1
        self.items = list(uris)
        return {"snapshot_id": "snapshot-write"}


@pytest.mark.parametrize(
    ("count", "expected_postwrite_item_calls"),
    ((49, 1), (50, 2), (51, 2), (75, 2), (100, 3)),
)
def test_attested_matching_snapshot_skips_only_prewrite_items(
    tmp_path: Path,
    count: int,
    expected_postwrite_item_calls: int,
) -> None:
    playlist = _playlist()
    desired = _desired(playlist, count)
    old = _desired(playlist, count, suffix="old")
    store = _store(tmp_path)
    _seed_attestation(store, playlist, old.uris)
    client = _Spotify(old.uris, ["snapshot-a", "snapshot-write"])

    result = reconcile_spotify_playlist(
        client,
        playlist,
        desired,
        store=store,
        snapshot_attested_prewrite=True,
    )

    assert result.ok
    assert result.wrote is True
    assert result.remote_evidence_qualified is True
    assert client.snapshot_calls == 2
    assert len(client.item_calls) == expected_postwrite_item_calls
    assert client.writes == 1
    attestation = store.get_playlist_attestation(playlist.id)
    assert attestation is not None
    assert attestation.snapshot_id == "snapshot-write"
    assert attestation.desired_fingerprint == playlist_attestation_fingerprint(desired.uris)


def test_changed_snapshot_falls_back_to_complete_prewrite_read(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist, 75)
    old = _desired(playlist, 75, suffix="old")
    store = _store(tmp_path)
    _seed_attestation(store, playlist, old.uris)
    client = _Spotify(old.uris, ["snapshot-c", "snapshot-c", "snapshot-write"])

    result = reconcile_spotify_playlist(
        client,
        playlist,
        desired,
        store=store,
        snapshot_attested_prewrite=True,
    )

    assert result.ok and result.wrote is True
    assert client.snapshot_calls == 3
    assert len(client.item_calls) == 4
    assert client.writes == 1


def test_snapshot_failure_falls_back_without_authorizing_the_shortcut(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist, 75)
    old = _desired(playlist, 75, suffix="old")
    store = _store(tmp_path)
    _seed_attestation(store, playlist, old.uris)
    client = _Spotify(
        old.uris,
        [
            SpotifyTransportError("snapshot optimization failure"),
            "snapshot-a",
            "snapshot-write",
        ],
    )

    result = reconcile_spotify_playlist(
        client,
        playlist,
        desired,
        store=store,
        snapshot_attested_prewrite=True,
    )

    assert result.ok and result.wrote is True
    assert client.snapshot_calls == 3
    assert len(client.item_calls) == 4
    assert client.writes == 1


def test_attestation_for_current_desired_never_authorizes_dirty_prewrite_shortcut(
    tmp_path: Path,
) -> None:
    playlist = _playlist()
    desired = _desired(playlist, 49)
    store = _store(tmp_path)
    _seed_attestation(store, playlist, desired.uris)
    client = _Spotify(desired.uris, ["snapshot-a"])

    result = reconcile_spotify_playlist(
        client,
        playlist,
        desired,
        store=store,
        snapshot_attested_prewrite=True,
    )

    assert result.ok and result.wrote is False
    assert client.snapshot_calls == 0
    assert len(client.item_calls) == 1
    assert client.writes == 0


def test_pending_confirmation_blocks_attested_prewrite_shortcut(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist, 49)
    old = _desired(playlist, 49, suffix="old")
    store = _store(tmp_path)
    _seed_attestation(store, playlist, old.uris)
    PendingSnapshotJournal(store).set(
        PendingSnapshotConfirmation(
            playlist_id=playlist.id,
            destination_id=playlist.destination.external_id,
            baseline_snapshot_id="snapshot-a",
            expected_snapshot_id="snapshot-b",
            desired_fingerprint=playlist_attestation_fingerprint(desired.uris),
            created_at=NOW - timedelta(minutes=2),
            expires_at=NOW + timedelta(minutes=58),
            readback_exact=False,
        )
    )
    client = _Spotify(old.uris, ["snapshot-a", "snapshot-write"])

    result = reconcile_spotify_playlist(
        client,
        playlist,
        desired,
        store=store,
        snapshot_attested_prewrite=True,
    )

    assert result.ok and result.wrote is True
    assert len(client.item_calls) == 2
    assert client.snapshot_calls == 2
    assert client.writes == 1


def test_prewrite_preparation_change_rechecks_snapshot_before_skipping_items(
    tmp_path: Path,
) -> None:
    playlist = _playlist()
    first = _desired(playlist, 49, suffix="first")
    final = _desired(playlist, 49, suffix="final")
    old = _desired(playlist, 49, suffix="old")
    store = _store(tmp_path)
    _seed_attestation(store, playlist, old.uris)
    client = _Spotify(old.uris, ["snapshot-a", "snapshot-a", "snapshot-write"])
    preparations: list[tuple[str, ...]] = []

    def prepare(current: DesiredPlaylistState) -> DesiredPlaylistState:
        preparations.append(current.uris)
        return final if current.items == first.items else current

    result = reconcile_spotify_playlist(
        client,
        playlist,
        first,
        store=store,
        prepare_write=prepare,
        prewrite_iteration_limit=3,
        snapshot_attested_prewrite=True,
    )

    assert result.ok and result.wrote is True
    assert preparations == [first.uris, final.uris]
    assert client.snapshot_calls == 3
    assert len(client.item_calls) == 1
    assert client.writes == 1


def _changed_context(
    current: DestinationVerificationContext,
    *,
    desired_fingerprint: str,
    config_fingerprint: str | None = None,
) -> DestinationVerificationContext:
    return DestinationVerificationContext(
        desired_fingerprint=desired_fingerprint,
        destination_context_fingerprint=current.destination_context_fingerprint,
        config_fingerprint=current.config_fingerprint if config_fingerprint is None else config_fingerprint,
        auth_context_fingerprint=current.auth_context_fingerprint,
    )


def test_engine_marks_only_compatible_dirty_desired_change_as_snapshot_candidate(
    tmp_path: Path,
) -> None:
    runner, _, _, _ = setup(tmp_path)
    assert runner.run_cycle().ok
    playlist = runner.config.playlists[0]
    state = runner.planner_journal.get_destination("first")
    assert state is not None and state.confirmed_desired_fingerprint is not None
    current = DestinationVerificationContext(
        desired_fingerprint=state.observed_desired_fingerprint,
        destination_context_fingerprint=state.destination_context_fingerprint,
        config_fingerprint=state.config_fingerprint,
        auth_context_fingerprint=state.auth_context_fingerprint,
    )
    changed = _changed_context(current, desired_fingerprint="e" * 64)
    runner.planner_journal.observe_destination(
        playlist_id="first",
        destination_context_fingerprint=changed.destination_context_fingerprint,
        config_fingerprint=changed.config_fingerprint,
        auth_context_fingerprint=changed.auth_context_fingerprint,
        desired_fingerprint=changed.desired_fingerprint,
    )

    plan = runner._destination_work_plan(playlist, NOW + timedelta(minutes=10), changed)

    assert plan.needs_remote is True
    assert plan.audit_only is False
    assert plan.snapshot_attested_prewrite is True


def test_engine_disables_snapshot_candidate_for_unknown_or_incompatible_state(
    tmp_path: Path,
) -> None:
    unknown_runner, _, _, _ = setup(tmp_path / "unknown")
    assert unknown_runner.run_cycle().ok
    playlist = unknown_runner.config.playlists[0]
    state = unknown_runner.planner_journal.get_destination("first")
    assert state is not None
    current = DestinationVerificationContext(
        desired_fingerprint=state.observed_desired_fingerprint,
        destination_context_fingerprint=state.destination_context_fingerprint,
        config_fingerprint=state.config_fingerprint,
        auth_context_fingerprint=state.auth_context_fingerprint,
    )
    changed = _changed_context(current, desired_fingerprint="e" * 64)
    unknown_runner.planner_journal.observe_destination(
        playlist_id="first",
        destination_context_fingerprint=changed.destination_context_fingerprint,
        config_fingerprint=changed.config_fingerprint,
        auth_context_fingerprint=changed.auth_context_fingerprint,
        desired_fingerprint=changed.desired_fingerprint,
    )
    unknown_runner.planner_journal.mark_destination_unknown("first")

    unknown_plan = unknown_runner._destination_work_plan(
        playlist,
        NOW + timedelta(minutes=10),
        changed,
    )

    assert unknown_plan.needs_remote is True
    assert unknown_plan.snapshot_attested_prewrite is False

    incompatible_runner, _, _, _ = setup(tmp_path / "incompatible")
    assert incompatible_runner.run_cycle().ok
    playlist = incompatible_runner.config.playlists[0]
    prior = incompatible_runner.planner_journal.get_destination("first")
    assert prior is not None
    incompatible = DestinationVerificationContext(
        desired_fingerprint="f" * 64,
        destination_context_fingerprint=prior.destination_context_fingerprint,
        config_fingerprint="a" * 64,
        auth_context_fingerprint=prior.auth_context_fingerprint,
    )
    incompatible_runner.planner_journal.observe_destination(
        playlist_id="first",
        destination_context_fingerprint=incompatible.destination_context_fingerprint,
        config_fingerprint=incompatible.config_fingerprint,
        auth_context_fingerprint=incompatible.auth_context_fingerprint,
        desired_fingerprint=incompatible.desired_fingerprint,
    )

    incompatible_plan = incompatible_runner._destination_work_plan(
        playlist,
        NOW + timedelta(minutes=10),
        incompatible,
    )

    assert incompatible_plan.needs_remote is True
    assert incompatible_plan.snapshot_attested_prewrite is False
