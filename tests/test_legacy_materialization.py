"""Pure legacy snapshot planning; no migration writes or Spotify traffic (#552)."""

from __future__ import annotations

import json
import socket
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from _legacy_materialization_fixtures import (
    PRODUCTS,
    _catalog_with_duration_exception,
    _legacy_state,
)

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.installation_identity import (
    InstallationDisposition,
    inspect_installation_root,
)
from news_bulletin_playlist.legacy_materialization import (
    LegacyProvenance,
    preview_legacy_materialization,
)
from news_bulletin_playlist.managed_state import (
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    compile_engine_config,
    effective_sources,
    source_definition,
)
from news_bulletin_playlist.models import (
    PublicReleaseMode,
    SourceId,
)


@pytest.fixture(autouse=True)
def no_network_or_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Legacy preview must not access provider, network or SQLite")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_legacy_catalog_preview_freezes_all_records_without_mutating_root(
    tmp_path: Path, schema: int
) -> None:
    catalog = _catalog_with_duration_exception()
    root = tmp_path / f"legacy-v{schema}"
    root.mkdir()
    store = ManagedStateStore(root / "managed-state.json", catalog=catalog)
    original = _legacy_state(schema, catalog)
    store.save(original)
    (root / "history.sqlite3").write_bytes(b"sqlite sentinel")
    (root / "oauth-private.json").write_bytes(b"oauth sentinel")
    (root / "partial-post-journal.json").write_bytes(b"pending sentinel")
    cover_dir = root / "custom-covers"
    cover_dir.mkdir()
    (cover_dir / "uploaded.jpg").write_bytes(b"cover sentinel")
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    assert inspect_installation_root(root).disposition is InstallationDisposition.LEGACY_COMPATIBLE
    restored = store.load_readonly()
    assert restored == original
    preview = preview_legacy_materialization(catalog, restored)
    assert preview.original_state == original
    assert preview.original_runtime == compile_engine_config(catalog, restored)
    assert len(preview.source_snapshots) == len(catalog.sources) + len(original.sources)
    assert len(preview.template_snapshots) == len(catalog.playlists) + len(original.templates)
    assert (
        sum(
            record.provenance is LegacyProvenance.IMAGE_PRESET
            for record in preview.source_snapshots
        )
        == 13
    )
    assert (
        sum(
            record.provenance is LegacyProvenance.IMAGE_PRESET
            for record in preview.template_snapshots
        )
        == 7
    )

    expected = effective_sources(catalog, restored)
    assert tuple(source_definition(record.installed) for record in preview.source_snapshots) == (
        expected
    )
    assert {p.id: p.destination for p in preview.original_state.playlists} == {
        p.id: p.destination for p in original.playlists
    }
    assert preview.original_state.quarantined_templates == original.quarantined_templates
    assert preview.original_state.source_overrides == original.source_overrides
    assert preview.original_state.playlists[2].enabled is False
    assert preview.original_state.playlists[-1].min_duration_seconds == 600
    assert preview.original_state.playlists[-1].max_duration_seconds == 2100
    noon = next(
        record.definition.duration_policy.exceptions[0]
        for record in preview.template_snapshots
        if str(record.definition.id) == PRODUCTS[0]
    )
    assert noon.edition_local_time.fold == 1 and noon.max_seconds == 2700
    rne = next(item for item in preview.source_snapshots if item.installed.id == "rne")
    assert rne.installed.public_release_mode is PublicReleaseMode.DISABLED
    if schema >= 8:
        disabled = next(
            item for item in preview.source_snapshots if item.installed.id == "installed_news"
        )
        assert disabled.provenance is LegacyProvenance.INSTALLATION
        assert disabled.installed.enabled is False
        assert SourceId("installed_news") in preview.original_state.playlists[-1].source_ids

    # The plan is inspection-only, including existing secret/journal/cover bytes.
    assert {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    } == before
    assert preview_legacy_materialization(catalog, store.load_readonly()) == preview
    assert json.loads(store.path.read_text(encoding="utf-8"))["schema_version"] == schema


def test_catalog_snapshot_does_not_follow_subsequent_image_preset_changes() -> None:
    catalog = _catalog_with_duration_exception()
    state = _legacy_state(10, catalog)
    preview = preview_legacy_materialization(catalog, state)
    first = preview.source_snapshots[0].installed
    spain = next(
        x.definition for x in preview.template_snapshots if str(x.definition.id) == PRODUCTS[0]
    )
    changed_source = replace(catalog.sources[0], display_name="changed next release")
    changed_template = replace(catalog.playlist(PRODUCTS[0]), description="changed in image")
    changed = BuiltInCatalog(
        (changed_source,) + catalog.sources[1:],
        tuple(changed_template if p.id == changed_template.id else p for p in catalog.playlists),
    )
    assert preview_legacy_materialization(changed, state).source_snapshots[0].installed != first
    assert first.display_name == catalog.sources[0].display_name
    assert spain.description == catalog.playlist(PRODUCTS[0]).description
    assert (
        next(
            x.definition
            for x in preview_legacy_materialization(changed, state).template_snapshots
            if str(x.definition.id) == PRODUCTS[0]
        ).description
        != spain.description
    )


def test_missing_managed_state_with_existing_sqlite_is_never_fresh(tmp_path: Path) -> None:
    (tmp_path / "history.sqlite3").write_bytes(b"legacy history")
    assert inspect_installation_root(tmp_path).disposition is (
        InstallationDisposition.LEGACY_COMPATIBLE
    )
    state_store = ManagedStateStore(tmp_path / "managed-state.json")
    plan = preview_legacy_materialization(BUILTIN_CATALOG, state_store.load_readonly())
    assert len(plan.source_snapshots) == 13 and len(plan.template_snapshots) == 7
    assert plan.original_state.playlists == ()
    assert not state_store.path.exists()


def test_unsupported_schema_refuses_preview_without_relabeling() -> None:
    with pytest.raises(ManagedStateError, match="v6"):
        preview_legacy_materialization(BUILTIN_CATALOG, ManagedState(schema_version=11))
