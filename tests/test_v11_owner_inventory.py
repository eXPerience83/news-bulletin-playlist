"""Durable v11 owner inventory is independent of old image and fails closed offline."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from _installed_fixtures import _installed
from _legacy_materialization_fixtures import (
    _catalog_with_duration_exception,
    _legacy_state,
)

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.owner_inventory import OwnerInventory
from news_bulletin_playlist.owner_source_order import OwnerSourceOrder
from news_bulletin_playlist.owner_template_records import OwnerTemplateSet
from news_bulletin_playlist.v11_operational_codec import (
    V11OperationalError,
    parse_v11_operational,
)
from news_bulletin_playlist.v11_operational_storage import V11OperationalStore


def _legacy_root(tmp_path: Path, version: int) -> tuple[Path, bytes]:
    catalog = _catalog_with_duration_exception()
    root = tmp_path / "install"
    root.mkdir()
    ManagedStateStore(root / "managed-state.json", catalog=catalog).save(
        _legacy_state(version, catalog)
    )
    raw = (root / "managed-state.json").read_bytes()
    (root / "old-private-token").write_bytes(b"token-should-stay-private")
    (root / "old-history.sqlite3").write_bytes(b"old-history-stays-untouched")
    return root, raw


@pytest.mark.parametrize("version", [6, 7, 8, 9, 10])
def test_first_conversion_persists_source_and_template_owner_inventory_without_remote_io(
    tmp_path: Path, version: int
):
    root, old_state_bytes = _legacy_root(tmp_path, version)
    owner = V11OperationalStore(root)
    first = owner.initialize_from_legacy(catalog=_catalog_with_duration_exception())
    assert first.owner_inventory is not None
    inventory = first.owner_inventory
    assert len(inventory.sources) == (14 if version < 8 else 15)
    assert len(inventory.templates.records) == len(BUILTIN_CATALOG.playlists)
    assert inventory.source_order.ids == tuple(s.id for s in first.runtime.sources)
    assert inventory.source_order.definitions(inventory.sources) == first.runtime.sources
    assert tuple(t.id for t in inventory.templates.records) == tuple(
        t.id for t in BUILTIN_CATALOG.playlists
    )
    assert inventory.templates.records[0].duration_policy.exceptions
    assert len(first.runtime.playlists) == 4

    stored_bytes = owner.load_bytes()
    stored = json.loads(stored_bytes)
    assert "owner_inventory" in stored
    assert len(stored["owner_inventory"]["installed_sources"]) == 1
    assert stored["owner_inventory"]["source_order"]["source_ids"] == list(
        inventory.source_order.ids
    )
    assert parse_v11_operational(stored_bytes).owner_inventory == inventory
    assert owner.initialize_from_legacy(catalog=_catalog_with_duration_exception()) == first
    assert owner.load_bytes() == stored_bytes
    assert (root / "managed-state.json").read_bytes() == old_state_bytes
    assert (root / "old-private-token").read_bytes() == b"token-should-stay-private"
    assert (root / "old-history.sqlite3").read_bytes() == b"old-history-stays-untouched"
    assert b"token-should-stay-private" not in stored_bytes


def test_owner_inventory_survives_managed_overlay_cas_without_rebuilding_image_defaults(
    tmp_path: Path,
):
    root, _ = _legacy_root(tmp_path, 10)
    owner = V11OperationalStore(root)
    previous = owner.initialize_from_legacy(catalog=_catalog_with_duration_exception())
    original = owner.load_bytes()
    assert previous.owner_inventory is not None
    changed_state = replace(
        previous.managed_overlay,
        playlists=tuple(replace(p, enabled=False) for p in previous.managed_overlay.playlists),
    )
    next_bytes = owner.save(changed_state, expected_bytes=original)
    reopened = V11OperationalStore(root).load()
    assert reopened.owner_inventory == previous.owner_inventory
    assert reopened.runtime != previous.runtime
    assert reopened.managed_overlay == changed_state
    assert reopened.migration_origin_sha256 == previous.migration_origin_sha256
    assert next_bytes != original


def test_empty_clean_install_has_empty_durable_owner_inventory(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    first = V11OperationalStore(root).initialize_fresh()
    assert first.owner_inventory == OwnerInventory(
        source_order=OwnerSourceOrder(()),
        sources=(),
        templates=OwnerTemplateSet(()),
    )
    document = json.loads(V11OperationalStore(root).load_bytes())
    assert document["owner_inventory"]["installed_sources"] == []
    assert document["owner_inventory"]["templates"]["templates"] == []
    assert first.runtime.sources == () and first.runtime.playlists == ()


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["owner_inventory"]["source_order"]["source_ids"].reverse(),
        lambda d: d["owner_inventory"]["source_order"]["source_ids"].pop(),
        lambda d: d["owner_inventory"]["installed_sources"][0]["sources"].pop(),
        lambda d: d["owner_inventory"]["templates"]["templates"].pop(),
        lambda d: d["owner_inventory"]["templates"]["templates"][0].update(
            {"retention_hours": 999}
        ),
        lambda d: d["owner_inventory"]["templates"]["templates"][0]["duration_policy"][
            "exceptions"
        ].clear(),
        lambda d: d["owner_inventory"]["installed_sources"][0]["sources"][0]["processing"][
            "parser"
        ].update({"id": "not-installed"}),
        lambda d: d.pop("owner_inventory"),
    ],
)
def test_corrupted_owner_inventory_cannot_silently_fall_back_to_image_preset(
    tmp_path: Path, change
):
    root, _ = _legacy_root(tmp_path, 10)
    owner = V11OperationalStore(root)
    owner.initialize_from_legacy(catalog=_catalog_with_duration_exception())
    doc = json.loads(owner.load_bytes())
    change(doc)
    raw = (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    with pytest.raises(V11OperationalError):
        parse_v11_operational(raw)
    # A malformed in-memory document cannot authorize an operational mutation.
    assert owner.load().owner_inventory is not None


def test_old_v11_beta_document_requires_explicit_conversion_not_hidden_fallback(tmp_path: Path):
    root, _ = _legacy_root(tmp_path, 10)
    owner = V11OperationalStore(root)
    first = owner.initialize_from_legacy(catalog=_catalog_with_duration_exception())
    doc = json.loads(owner.load_bytes())
    del doc["owner_inventory"]
    old_bytes = (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    with pytest.raises(V11OperationalError, match="owner inventory"):
        parse_v11_operational(old_bytes)
    assert first.owner_inventory is not None


def test_portable_capability_chunks_cover_more_than_one_hundred_owner_sources():
    # The owner *operational* inventory must not inherit the 100-source
    # limit of an individual portable source v1 document.
    model = _installed()
    records = tuple(
        replace(model, id=SourceId(f"owner_{i:03d}"), spotify_show_id=f"{i:022d}")
        for i in range(101)
    )
    order = OwnerSourceOrder(tuple(s.id for s in records))
    from news_bulletin_playlist.owner_inventory import OwnerInventory

    inventory = OwnerInventory(order, records, OwnerTemplateSet(()))
    document = inventory.to_document()
    assert len(document["installed_sources"]) == 2
    roundtripped = OwnerInventory.parse(document)
    assert roundtripped.source_order.definitions(roundtripped.sources) == order.definitions(records)
