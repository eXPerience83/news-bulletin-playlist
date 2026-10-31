"""Forward-only v11 owner writes remain independent of old state and network."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import replace
from pathlib import Path

import pytest
from test_legacy_catalog_snapshot import _root

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_state import (
    ManagedSourceOverride,
    ManagedStateStore,
)
from news_bulletin_playlist.models import PublicReleaseMode
from news_bulletin_playlist.v11_operational_codec import (
    V11OperationalError,
    parse_v11_operational,
    update_v11_operational,
)
from news_bulletin_playlist.v11_operational_storage import (
    V11_OPERATIONAL_FILENAME,
    V11OperationalStore,
    V11StoreError,
)


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_one_shot_forward_upgrade_then_edit_restart_and_preserve_legacy(
    tmp_path: Path, schema: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, preview = _root(tmp_path, schema)
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}

    def no_network(*args: object, **kwargs: object) -> None:
        pytest.fail("v11 owner store contacted external provider")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    store = V11OperationalStore(root)
    first = store.initialize_from_legacy(catalog=BUILTIN_CATALOG)
    assert first.runtime == preview.original_runtime
    assert store.initialize_from_legacy() == first
    assert first.managed_overlay == preview.original_state
    assert len(first.managed_overlay.playlists) == 4
    assert (root / V11_OPERATIONAL_FILENAME).stat().st_mode & 0o777 == 0o600
    assert {
        p.name: p.read_bytes()
        for p in root.iterdir()
        if p.is_file() and p.name != V11_OPERATIONAL_FILENAME
    } == before
    previous = store.load_bytes()
    playlists = (
        replace(
            first.managed_overlay.playlists[0],
            display_name="Updated v11 title",
            max_episodes=19,
        ),
        *first.managed_overlay.playlists[1:],
    )
    revised = replace(first.managed_overlay, playlists=playlists)
    fresh = store.save(revised, expected_bytes=previous)
    assert fresh != previous
    assert V11OperationalStore(root).load().managed_overlay == revised
    assert store.load().historical_catalog == first.historical_catalog
    assert store.load().migration_origin_sha256 == first.migration_origin_sha256
    assert store.save(revised, expected_bytes=fresh) == fresh
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file() if p.name != V11_OPERATIONAL_FILENAME} == before
    with pytest.raises(V11StoreError, match="stale"):
        store.save(first.managed_overlay, expected_bytes=previous)


def test_valid_built_in_source_override_edit_does_not_conflict_with_frozen_catalog(
    tmp_path: Path,
) -> None:
    root, _ = _root(tmp_path, 10)
    store = V11OperationalStore(root)
    old = store.initialize_from_legacy()
    original_bytes = store.load_bytes()
    item = old.historical_catalog.sources[0]
    state = replace(
        old.managed_overlay,
        source_overrides=(ManagedSourceOverride(item.id, PublicReleaseMode.INSTANT),),
    )
    try:
        new_payload = update_v11_operational(original_bytes, state)
    except V11OperationalError as exc:
        # Some catalog entries already have exactly this override by default;
        # the edited overlay must still be valid in the current catalog.
        pytest.fail(f"v11 source override rejected: {exc}")
    assert parse_v11_operational(new_payload).managed_overlay == state
    assert store.save(state, expected_bytes=original_bytes) == new_payload


def test_missing_or_corrupt_v11_never_falls_back_to_old_file(tmp_path: Path) -> None:
    root, _ = _root(tmp_path)
    store = V11OperationalStore(root)
    with pytest.raises(V11StoreError, match="not initialized"):
        store.load()
    store.initialize_from_legacy()
    (root / V11_OPERATIONAL_FILENAME).write_bytes(b"{ malformed json")
    with pytest.raises(V11StoreError, match="never fall back"):
        store.load()
    with pytest.raises(V11StoreError, match="never fall back"):
        store.initialize_from_legacy()
    assert ManagedStateStore(root / "managed-state.json").load_readonly().playlists


def test_interrupted_atomic_publication_preserves_legacy_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _root(tmp_path)
    legacy = (root / "managed-state.json").read_bytes()

    def block_link(*args: object, **kwargs: object) -> None:
        raise OSError("injected disk failure")

    monkeypatch.setattr(os, "link", block_link)
    with pytest.raises(V11StoreError, match="initialization failed"):
        V11OperationalStore(root).initialize_from_legacy()
    assert not (root / V11_OPERATIONAL_FILENAME).exists()
    assert (root / "managed-state.json").read_bytes() == legacy


def test_write_failure_retains_existing_v11_and_rejects_unsafe_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _root(tmp_path)
    store = V11OperationalStore(root)
    old = store.initialize_from_legacy()
    baseline = store.load_bytes()
    changed = replace(
        old.managed_overlay,
        playlists=(
            replace(old.managed_overlay.playlists[0], description="fresh"),
            *old.managed_overlay.playlists[1:],
        ),
    )
    original_replace = os.replace

    def block_replace(*args: object, **kwargs: object) -> None:
        raise OSError("injected replacement failure")

    monkeypatch.setattr(os, "replace", block_replace)
    with pytest.raises(V11StoreError, match="durability uncertain"):
        store.save(changed, expected_bytes=baseline)
    assert store.load_bytes() == baseline
    monkeypatch.setattr(os, "replace", original_replace)
    with pytest.raises(V11StoreError, match="invalid v11"):
        store.save(replace(old.managed_overlay, schema_version=5), expected_bytes=baseline)
    assert store.load_bytes() == baseline


def test_corrupt_document_and_unknown_fields_are_rejected(tmp_path: Path) -> None:
    root, _ = _root(tmp_path)
    store = V11OperationalStore(root)
    store.initialize_from_legacy()
    payload = json.loads(store.load_bytes())
    payload["unrecognized"] = True
    (root / V11_OPERATIONAL_FILENAME).write_bytes(
        (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    )
    with pytest.raises(V11StoreError, match="never fall back"):
        store.load()
