"""Frozen v2 is additive: exact source order, v1 compatibility, safe publication."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from test_installed_sources import _installed
from test_legacy_catalog_snapshot import _root

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.legacy_catalog_snapshot import (
    FROZEN_LEGACY_SNAPSHOT_FILENAME,
    FROZEN_LEGACY_SNAPSHOT_V2_FILENAME,
    FrozenLegacySnapshotError,
    build_legacy_snapshot,
    build_legacy_snapshot_v2,
    parse_legacy_snapshot,
    write_legacy_snapshot,
    write_legacy_snapshot_v2,
)
from news_bulletin_playlist.legacy_materialization import (
    LegacyProvenance,
    preview_legacy_materialization,
)
from news_bulletin_playlist.managed_state import ManagedSourceOverride, ManagedStateStore
from news_bulletin_playlist.models import PublicReleaseMode, SourceId
from news_bulletin_playlist.v11_projection import project_v11_legacy


def _files(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_v2_restores_exact_order_and_four_destinations(tmp_path: Path, schema: int) -> None:
    root, preview = _root(tmp_path, schema)
    before = _files(root)
    state_bytes = before["managed-state.json"]
    v1 = build_legacy_snapshot(preview, state_bytes)
    v2 = build_legacy_snapshot_v2(preview, state_bytes)
    parsed_old = parse_legacy_snapshot(v1)
    parsed_new = parse_legacy_snapshot(v2)
    assert parsed_old.source_order is None
    assert parsed_new.source_order == tuple(
        item.id for item in preview.original_runtime.sources
    )
    assert parsed_new.sources == parsed_old.sources
    assert parsed_new.templates == parsed_old.templates
    assert project_v11_legacy(v1, state_bytes).source_order_verified is False
    projection = project_v11_legacy(v2, state_bytes)
    assert projection.source_order_verified is True
    assert projection.projected_runtime == preview.original_runtime
    assert projection.original_state == preview.original_state
    assert projection.managed_destinations == preview.original_state.playlists
    assert tuple(s.id for s in projection.installed_sources) == parsed_new.source_order
    assert _files(root) == before

    assert write_legacy_snapshot(root, preview) == v1
    assert write_legacy_snapshot_v2(root, preview) == v2
    assert write_legacy_snapshot_v2(root, preview) == v2
    after = _files(root)
    assert after[FROZEN_LEGACY_SNAPSHOT_FILENAME] == v1
    assert after[FROZEN_LEGACY_SNAPSHOT_V2_FILENAME] == v2
    assert {k: v for k, v in after.items() if k not in {
        FROZEN_LEGACY_SNAPSHOT_FILENAME, FROZEN_LEGACY_SNAPSHOT_V2_FILENAME
    }} == before
    assert not any(name.endswith(".tmp") for name in after)


def test_v2_respects_disabled_custom_source_and_builtin_override(tmp_path: Path) -> None:
    root, _ = _root(tmp_path, 10)
    store = ManagedStateStore(root / "managed-state.json")
    state = replace(
        store.load_readonly(),
        sources=(_installed(enabled=False),),
        source_overrides=(ManagedSourceOverride(SourceId("rne"), PublicReleaseMode.DISABLED),),
    )
    store.save(state)
    before = _files(root)
    preview = preview_legacy_materialization(BUILTIN_CATALOG, state)
    payload = build_legacy_snapshot_v2(preview, before["managed-state.json"])
    actual = project_v11_legacy(payload, before["managed-state.json"])
    assert actual.source_order_verified
    assert actual.projected_runtime == preview.original_runtime
    assert actual.source_provenance.count(LegacyProvenance.INSTALLATION) == 1
    assert actual.installed_sources[-1].id == SourceId("installed_news")
    assert actual.installed_sources[-1].enabled is False
    assert _files(root) == before


@pytest.mark.parametrize(
    "order",
    [
        [],
        ["rne", "rne"],
        ["unknown"],
        [True],
        "not-a-list",
    ],
)
def test_invalid_v2_source_order_fails_closed(tmp_path: Path, order: object) -> None:
    root, preview = _root(tmp_path)
    before = _files(root)
    document = json.loads(build_legacy_snapshot_v2(preview, before["managed-state.json"]))
    document["source_order"] = order
    with pytest.raises(FrozenLegacySnapshotError, match="source order"):
        parse_legacy_snapshot(json.dumps(document).encode("utf-8"))
    assert _files(root) == before


def test_v1_with_v2_order_field_is_not_accepted(tmp_path: Path) -> None:
    root, preview = _root(tmp_path)
    document = json.loads(
        build_legacy_snapshot(preview, (root / "managed-state.json").read_bytes())
    )
    document["source_order"] = [str(s.installed.id) for s in preview.source_snapshots]
    with pytest.raises(FrozenLegacySnapshotError):
        parse_legacy_snapshot(json.dumps(document).encode("utf-8"))


def test_v2_never_overwrites_bad_or_conflicting_old_v1(tmp_path: Path) -> None:
    root, preview = _root(tmp_path)
    marker = root / FROZEN_LEGACY_SNAPSHOT_FILENAME
    marker.write_bytes(b"corrupt old evidence")
    with pytest.raises(FrozenLegacySnapshotError):
        write_legacy_snapshot_v2(root, preview)
    assert marker.read_bytes() == b"corrupt old evidence"
    assert not (root / FROZEN_LEGACY_SNAPSHOT_V2_FILENAME).exists()


def test_v2_publication_failure_preserves_all_original_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, preview = _root(tmp_path)
    before = _files(root)

    def fail_link(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic exclusive-link failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(FrozenLegacySnapshotError, match="publication uncertain"):
        write_legacy_snapshot_v2(root, preview)
    assert _files(root) == before
