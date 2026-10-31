"""Read-only v11 projection must not drift or touch any live installation data."""

from __future__ import annotations

import socket
import sqlite3
from pathlib import Path

import pytest
from test_legacy_catalog_snapshot import _root
from test_legacy_materialization import _catalog_with_duration_exception, _legacy_state

from news_bulletin_playlist.legacy_catalog_snapshot import (
    build_legacy_snapshot_v2,
    write_legacy_snapshot_v2,
)
from news_bulletin_playlist.legacy_materialization import preview_legacy_materialization
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.v11_candidate import (
    V11CandidateError,
    build_v11_candidate,
)
from news_bulletin_playlist.v11_candidate_storage import publish_v11_candidate
from news_bulletin_playlist.v11_state_reader import (
    V11_STATE_SCHEMA_VERSION,
    load_v11_readonly_state,
    parse_v11_readonly_state,
)


@pytest.fixture(autouse=True)
def no_network_or_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("read-only v11 state must not contact Spotify or SQLite")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("version", [6, 7, 8, 9, 10])
def test_standalone_v11_reader_preserves_exact_runtime_and_destinations(
    tmp_path: Path, version: int
) -> None:
    root, preview = _root(tmp_path, version)
    legacy = (root / "managed-state.json").read_bytes()
    encoded = build_v11_candidate(build_legacy_snapshot_v2(preview, legacy), legacy)
    before = _files(root)
    state = parse_v11_readonly_state(encoded)

    assert state.schema_version == V11_STATE_SCHEMA_VERSION
    assert state.runtime == preview.original_runtime
    assert len(state.installed_sources) == 13
    assert len(state.playlist_examples) == 7
    assert state.managed_destinations == preview.original_state.playlists
    assert len(state.managed_destinations) == 4
    assert state.local_drafts == preview.original_state.templates
    assert state.quarantined_drafts == preview.original_state.quarantined_templates
    assert state.legacy_managed_state_bytes == legacy
    assert state.is_live_migration_authorized is False
    assert _files(root) == before

    write_legacy_snapshot_v2(root, preview)
    publish_v11_candidate(root)
    published = _files(root)
    disk_state = load_v11_readonly_state(root)
    assert disk_state == state
    assert _files(root) == published
    # The actual legacy managed-state reader remains untouched and unchanged.
    assert ManagedStateStore(root / "managed-state.json").load_readonly() == (
        preview.original_state
    )


def test_v11_view_retains_custom_drafts_overrides_dst_and_dormant_sources(
    tmp_path: Path,
) -> None:
    catalog = _catalog_with_duration_exception()
    root = tmp_path / "legacy"
    root.mkdir()
    old = _legacy_state(10, catalog)
    store = ManagedStateStore(root / "managed-state.json", catalog=catalog)
    store.save(old)
    before = _files(root)
    raw = before["managed-state.json"]
    preview = preview_legacy_materialization(catalog, store.load_readonly())
    state = parse_v11_readonly_state(
        build_v11_candidate(build_legacy_snapshot_v2(preview, raw), raw)
    )
    assert state.runtime == preview.original_runtime
    assert state.source_overrides == old.source_overrides
    assert state.quarantined_drafts == old.quarantined_templates
    assert state.local_drafts == old.templates
    assert state.managed_destinations == old.playlists
    assert len(state.installed_sources) == len(preview.source_snapshots)
    assert state.managed_destinations[-1].source_ids == old.playlists[-1].source_ids
    spain = next(item for item in state.playlist_examples if str(item.id) == "spain_spanish_news")
    assert spain.duration_policy.exceptions[0].edition_local_time.fold == 1
    assert _files(root) == before


def test_readonly_recovery_never_overwrites_changed_legacy_state(tmp_path: Path) -> None:
    root, preview = _root(tmp_path)
    write_legacy_snapshot_v2(root, preview)
    publish_v11_candidate(root)
    original = (root / "managed-state.json").read_bytes()
    (root / "managed-state.json").write_bytes(b"new simulated managed state")
    before = _files(root)
    with pytest.raises(V11CandidateError, match="original managed state has changed"):
        load_v11_readonly_state(root)
    recovered = load_v11_readonly_state(root, recovery_inspection=True)
    assert recovered.legacy_managed_state_bytes == original
    assert recovered.runtime == preview.original_runtime
    assert recovered.is_live_migration_authorized is False
    assert _files(root) == before


def test_no_candidate_does_not_create_any_v11_file(tmp_path: Path) -> None:
    root, _ = _root(tmp_path)
    before = _files(root)
    with pytest.raises(V11CandidateError):
        load_v11_readonly_state(root)
    assert _files(root) == before


def test_invalid_v11_input_cannot_fall_back_to_legacy_defaults(tmp_path: Path) -> None:
    root, preview = _root(tmp_path)
    raw = (root / "managed-state.json").read_bytes()
    before = _files(root)
    with pytest.raises(V11CandidateError):
        parse_v11_readonly_state(
            build_v11_candidate(build_legacy_snapshot_v2(preview, raw), raw) + b" "
        )
    assert _files(root) == before
