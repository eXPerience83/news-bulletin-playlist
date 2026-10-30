"""Isolated v6–v10 frozen catalogue sidecars, no live provider or startup I/O."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.legacy_catalog_snapshot import (
    FROZEN_LEGACY_SNAPSHOT_FILENAME,
    FrozenLegacySnapshotError,
    build_legacy_snapshot,
    parse_legacy_snapshot,
    write_legacy_snapshot,
)
from news_bulletin_playlist.legacy_materialization import (
    LegacyProvenance,
    preview_legacy_materialization,
)
from news_bulletin_playlist.managed_state import (
    ManagedState,
    ManagedStateStore,
    activate_template,
)


def _root(tmp_path: Path, schema: int = 6):
    root = tmp_path / "legacy"
    root.mkdir()
    original = ManagedState(
        schema_version=schema,
        playlists=tuple(
            replace(
                activate_template(template, chr(65 + index) * 22),
                enabled=index != 2,
                source_ids=template.default_source_ids,
            )
            for index, template in enumerate(
                template
                for template in BUILTIN_CATALOG.playlists
                if str(template.id)
                in {
                    "spain_spanish_news",
                    "international_english_news",
                    "international_polish_news",
                    "international_spanish_long_news",
                }
            )
        ),
    )
    state = ManagedStateStore(root / "managed-state.json")
    state.save(original)
    (root / "oauth-private").write_bytes(b"secret-oauth")
    (root / "history.sqlite3").write_bytes(b"secret-history")
    (root / "pending-post.json").write_bytes(b"pending-post")
    preview = preview_legacy_materialization(BUILTIN_CATALOG, state.load_readonly())
    return root, preview


def _files(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_frozen_sidecar_exact_sources_and_templates_without_state_changes(
    tmp_path: Path, schema: int
) -> None:
    root, preview = _root(tmp_path, schema)
    before = _files(root)
    raw = before["managed-state.json"]
    payload = build_legacy_snapshot(preview, raw)
    assert build_legacy_snapshot(preview, raw) == payload
    decoded = parse_legacy_snapshot(payload)
    assert decoded.managed_schema_version == schema
    assert len(decoded.sources) == 13
    assert len(decoded.templates) == 7
    assert all(s.provenance is LegacyProvenance.IMAGE_PRESET for s in decoded.sources)
    assert all(t.provenance is LegacyProvenance.IMAGE_PRESET for t in decoded.templates)
    assert {s.installed.id for s in decoded.sources} == {
        s.installed.id for s in preview.source_snapshots
    }
    assert {s.installed for s in decoded.sources} == {
        s.installed for s in preview.source_snapshots
    }
    assert decoded.templates == preview.template_snapshots
    assert b"secret-oauth" not in payload
    assert b"secret-history" not in payload
    assert b"pending-post" not in payload
    assert b"AAAAAAAAAAAAAAAAAAAAAA" not in payload
    assert _files(root) == before

    assert write_legacy_snapshot(root, preview) == payload
    assert write_legacy_snapshot(root, preview) == payload
    after = _files(root)
    assert {
        key: value for key, value in after.items() if key != FROZEN_LEGACY_SNAPSHOT_FILENAME
    } == before
    assert after[FROZEN_LEGACY_SNAPSHOT_FILENAME] == payload
    assert not any(".tmp" in x.name for x in root.iterdir())


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(managed_schema_version=True),
        lambda d: d.update(managed_state_sha256="A" * 64),
        lambda d: d.update(unexpected="field"),
        lambda d: d.update(sources=[]),
        lambda d: d.update(templates=[{"id": "bad"}]),
    ],
)
def test_corrupt_sidecar_data_is_rejected(tmp_path: Path, change) -> None:
    root, preview = _root(tmp_path)
    data = json.loads(build_legacy_snapshot(preview, (root / "managed-state.json").read_bytes()))
    change(data)
    bad = (json.dumps(data) + "\n").encode("utf-8")
    with pytest.raises(FrozenLegacySnapshotError):
        parse_legacy_snapshot(bad)


def test_duplicate_json_field_is_rejected() -> None:
    with pytest.raises(FrozenLegacySnapshotError):
        parse_legacy_snapshot(b'{"format":"a","format":"b"}')


def test_mutated_legacy_root_refuses_to_overwrite_existing_snapshot(tmp_path: Path) -> None:
    root, preview = _root(tmp_path)
    before = write_legacy_snapshot(root, preview)
    state = root / "managed-state.json"
    state.write_bytes(state.read_bytes() + b" ")
    with pytest.raises(FrozenLegacySnapshotError, match="differs"):
        write_legacy_snapshot(root, preview)
    assert (root / FROZEN_LEGACY_SNAPSHOT_FILENAME).read_bytes() == before


def test_corrupted_existing_snapshot_is_not_overwritten(tmp_path: Path) -> None:
    root, preview = _root(tmp_path)
    marker = root / FROZEN_LEGACY_SNAPSHOT_FILENAME
    marker.write_bytes(b"not JSON")
    with pytest.raises(FrozenLegacySnapshotError, match="differs"):
        write_legacy_snapshot(root, preview)
    assert marker.read_bytes() == b"not JSON"


def test_atomic_publication_failure_preserves_original_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, preview = _root(tmp_path)
    before = _files(root)

    def no_link(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic linking failure")

    monkeypatch.setattr(os, "link", no_link)
    with pytest.raises(FrozenLegacySnapshotError, match="publication uncertain"):
        write_legacy_snapshot(root, preview)
    assert _files(root) == before


def test_unsafe_symlink_evidence_is_rejected(tmp_path: Path) -> None:
    root, preview = _root(tmp_path)
    (root / FROZEN_LEGACY_SNAPSHOT_FILENAME).symlink_to(root / "managed-state.json")
    with pytest.raises(FrozenLegacySnapshotError):
        write_legacy_snapshot(root, preview)
    assert (root / "managed-state.json").is_file()
