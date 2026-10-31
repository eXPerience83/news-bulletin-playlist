"""Forward-only runtime and Admin share the installed v11 catalog and state."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_legacy_catalog_snapshot import _root

from news_bulletin_playlist.effective_config import load_effective_config
from news_bulletin_playlist.managed_state import ManagedStateError, ManagedStateStore
from news_bulletin_playlist.v11_managed_adapter import (
    ForwardOnlyManagedStateStore,
    open_managed_state_store,
)
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME


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
    assert admin_store.catalog.sources == tuple(preview.original_runtime.sources)
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


def test_no_existing_installation_keeps_legacy_first_run_entrypoint(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    store = open_managed_state_store(root)
    assert type(store) is ManagedStateStore
    assert load_effective_config(root, {}) is None
    assert not (root / V11_OPERATIONAL_FILENAME).exists()
