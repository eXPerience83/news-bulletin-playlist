"""Forward-only runtime and Admin share the installed v11 catalog and state."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_legacy_catalog_snapshot import _root

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.effective_config import load_effective_config
from news_bulletin_playlist.managed_state import ManagedState, ManagedStateError, ManagedStateStore
from news_bulletin_playlist.v11_managed_adapter import (
    ForwardOnlyManagedStateStore,
    open_managed_state_store,
)
from news_bulletin_playlist.v11_operational_storage import (
    V11_AUTHORITY_FILENAME,
    V11_OPERATIONAL_FILENAME,
)


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_runtime_and_admin_use_exact_same_owned_state_after_upgrade(
    tmp_path: Path, schema: int
) -> None:
    root, preview = _root(tmp_path, schema)
    before = (root / "managed-state.json").read_bytes()
    effective = load_effective_config(root, {})
    assert effective == preview.original_runtime
    admin_store = open_managed_state_store(root)
    assert isinstance(admin_store, ForwardOnlyManagedStateStore)
    assert tuple(s.id for s in admin_store.catalog.sources) == tuple(
        s.id for s in preview.original_runtime.sources
    )
    original = admin_store.load_readonly()
    assert original == preview.original_state
    first = original.playlists[0]
    edited = replace(
        original,
        playlists=(replace(first, display_name="Live owned v11"), *original.playlists[1:]),
    )
    admin_store.save(edited)
    assert load_effective_config(root, {}) is not None
    assert open_managed_state_store(root).load() == edited
    assert (root / "managed-state.json").read_bytes() == before
    assert (root / V11_OPERATIONAL_FILENAME).exists()


def test_invalid_v11_blocks_legacy_reader_and_future_reupgrade(tmp_path: Path) -> None:
    root, _ = _root(tmp_path)
    assert load_effective_config(root, {}) is not None
    (root / V11_OPERATIONAL_FILENAME).write_bytes(b"broken v11")
    with pytest.raises(RuntimeError, match="invalid managed configuration"):
        load_effective_config(root, {})
    with pytest.raises(ManagedStateError, match="conversion"):
        open_managed_state_store(root)
    assert (root / "managed-state.json").exists()


def test_empty_first_run_goes_directly_to_v11_without_destination(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    assert load_effective_config(root, {}) is None
    store = open_managed_state_store(root)
    assert isinstance(store, ForwardOnlyManagedStateStore)
    assert store.load().playlists == ()
    assert store.load().sources == ()
    assert store.catalog.sources == ()
    assert store.catalog.playlists == ()
    assert (root / V11_OPERATIONAL_FILENAME).exists()
    assert not (root / "managed-state.json").exists()
    assert load_effective_config(root, {}) is None
    assert open_managed_state_store(root).load().playlists == ()


def test_missing_v11_after_activation_never_reimports_legacy(tmp_path: Path) -> None:
    root, _ = _root(tmp_path)
    assert load_effective_config(root, {}) is not None
    before = (root / "managed-state.json").read_bytes()
    assert (root / V11_AUTHORITY_FILENAME).is_file()
    (root / V11_OPERATIONAL_FILENAME).unlink()
    with pytest.raises(RuntimeError, match="invalid managed configuration"):
        load_effective_config(root, {})
    with pytest.raises(ManagedStateError, match="conversion"):
        open_managed_state_store(root)
    assert (root / "managed-state.json").read_bytes() == before
    assert not (root / V11_OPERATIONAL_FILENAME).exists()


def test_legacy_writers_and_readers_are_retired_after_cutover(tmp_path: Path) -> None:
    root, _ = _root(tmp_path)
    legacy = ManagedStateStore(root / "managed-state.json")
    before = legacy.path.read_bytes()
    assert open_managed_state_store(root).load().playlists
    with pytest.raises(ManagedStateError, match="retired"):
        legacy.save(ManagedState())
    with pytest.raises(ManagedStateError, match="retired"):
        legacy.load_readonly()
    assert legacy.path.read_bytes() == before


def test_stale_v11_writer_cannot_overwrite_newer_commit(tmp_path: Path) -> None:
    root, _ = _root(tmp_path)
    first = open_managed_state_store(root)
    second = open_managed_state_store(root)
    stale = second.load()
    current = first.load()
    first.save(
        replace(
            current,
            playlists=(
                replace(current.playlists[0], display_name="New"),
                *current.playlists[1:],
            ),
        )
    )
    with pytest.raises(ManagedStateError, match="could not be saved"):
        second.save(stale)
    assert open_managed_state_store(root).load().playlists[0].display_name == "New"



def test_fresh_install_never_materializes_bundled_source_presets(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    store = open_managed_state_store(root)
    assert store.catalog.sources == ()
    assert store.catalog.playlists == ()
    assert store.load().sources == ()
    assert BUILTIN_CATALOG.sources
    assert open_managed_state_store(root).catalog.sources == ()


def test_unknown_occupied_root_must_not_be_reclaimed_fresh(tmp_path: Path) -> None:
    prior = tmp_path / "historical-data.sqlite3"
    prior.write_bytes(b"prior installation")
    with pytest.raises(ManagedStateError, match="conversion"):
        open_managed_state_store(tmp_path)
    assert prior.read_bytes() == b"prior installation"
    assert not (tmp_path / V11_OPERATIONAL_FILENAME).exists()
