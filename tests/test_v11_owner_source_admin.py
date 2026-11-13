"""Admin owner inventory edits operate locally and retain historical processing capabilities."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from _admin_http import csrf as _csrf
from _admin_http import login as _login
from _admin_http import request as _request
from _installed_fixtures import _installed
from _legacy_dataset import legacy_root as _root
from _managed_fixtures import _source
from _source_admin_http_fixtures import _server

from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal
from news_bulletin_playlist.managed_state import (
    ManagedSourceOverride,
    effective_sources,
    source_definition,
)
from news_bulletin_playlist.models import PublicReleaseMode
from news_bulletin_playlist.portable_sources import export_sources, parse_sources
from news_bulletin_playlist.source_admin import export_source_configuration
from news_bulletin_playlist.source_import import SourceImportService
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_codec import parse_v11_operational
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME


def test_admin_edits_historical_source_metadata_without_changing_parser_or_spotify(
    tmp_path: Path,
) -> None:
    root, original = _root(tmp_path)
    before = (root / "managed-state.json").read_bytes()
    with _server(root) as (server, client, _legacy_store):
        store = open_managed_state_store(root)
        service = server.RequestHandlerClass.source_admin_service
        managed = server.RequestHandlerClass.managed_admin_service
        assert service is not None and managed is not None
        service.store = managed.store = store
        service.catalog = managed.catalog = store.catalog
        source = store.catalog.sources[0]
        source_id = str(source.id)
        cookie = _login(server)
        status, _, page = _request(
            server, "GET", f"/admin/sources/edit?source_id={source_id}", cookie=cookie
        )
        assert status == 200
        assert b"Edit installation source" in page
        assert b"Edit public release time" not in page
        status, _, _ = _request(
            server,
            "POST",
            "/admin/sources/update",
            cookie=cookie,
            form={
                "csrf_token": _csrf(page),
                "source_id": source_id,
                "display_name": "Owner renamed station",
                "origin_kind": source.origin.kind.value,
                "origin_code": source.origin.code,
                "language": str(source.languages[0]),
                "editorial_scope": source.editorial_scope.value,
                "temporal_strategy": source.temporal_strategy.value,
                "public_release_mode": source.public_release_mode.value,
            },
        )
        assert status == 303
        assert not client.calls
        current = open_managed_state_store(root)
        renamed = current.catalog.sources[0]
        assert renamed.display_name == "Owner renamed station"
        assert renamed.id == source.id
        assert renamed.parser_id == source.parser_id
        assert renamed.collection_filter_id == source.collection_filter_id
        assert renamed.temporal_strategy == source.temporal_strategy
        assert renamed.external_references == source.external_references
        assert tuple(s.id for s in current.catalog.sources) == tuple(
            s.id for s in store.catalog.sources
        )
        assert (root / "managed-state.json").read_bytes() == before
        doc = json.loads((root / V11_OPERATIONAL_FILENAME).read_text())
        assert doc["owner_revision"] == 1
        status, _, sources_page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        assert b"Owner renamed station" in sources_page
        assert b"Owner-managed" in sources_page
    assert len(original.original_runtime.playlists) == 4


def test_owner_source_disable_and_enable_preserves_assignments_and_detectors(
    tmp_path: Path,
) -> None:
    root, _preview = _root(tmp_path, 10)
    legacy_bytes = (root / "managed-state.json").read_bytes()
    with _server(root) as (server, client, _unused):
        store = open_managed_state_store(root)
        first, second = store.catalog.sources[:2]
        state = store.load_readonly()
        # Every active synthetic destination keeps another enabled source.
        store.save(
            replace(
                state,
                playlists=tuple(
                    replace(playlist, source_ids=(first.id, second.id))
                    for playlist in state.playlists
                ),
            )
        )
        source_service = server.RequestHandlerClass.source_admin_service
        managed = server.RequestHandlerClass.managed_admin_service
        assert source_service is not None and managed is not None
        source_service.store = managed.store = store
        source_service.catalog = managed.catalog = store.catalog
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        assert b"Owner-managed" in page
        status, _, _ = _request(
            server,
            "POST",
            "/admin/sources/set-enabled",
            cookie=cookie,
            form={
                "csrf_token": _csrf(page),
                "source_id": str(first.id),
                "enabled": "0",
            },
        )
        assert status == 303
        disabled = open_managed_state_store(root)
        assert not disabled.catalog.sources[0].enabled
        assert disabled.catalog.sources[0].parser_id == first.parser_id
        assert disabled.catalog.sources[0].collection_filter_id == first.collection_filter_id
        assert disabled.catalog.sources[0].temporal_strategy == first.temporal_strategy
        operational = parse_v11_operational((root / V11_OPERATIONAL_FILENAME).read_bytes())
        assert all(
            first.id not in playlist.source_selection.explicit
            and second.id in playlist.source_selection.explicit
            for playlist in operational.runtime.playlists
        )
        assert all(first.id in playlist.source_ids for playlist in disabled.load().playlists)
        assert (root / "managed-state.json").read_bytes() == legacy_bytes
        assert not client.calls

        status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        assert b"Disabled" in page
        status, _, _ = _request(
            server,
            "POST",
            "/admin/sources/set-enabled",
            cookie=cookie,
            form={
                "csrf_token": _csrf(page),
                "source_id": str(first.id),
                "enabled": "1",
            },
        )
        assert status == 303
        reopened = open_managed_state_store(root)
        assert reopened.catalog.sources[0].enabled
        assert all(
            first.id in playlist.source_selection.explicit
            for playlist in parse_v11_operational(
                (root / V11_OPERATIONAL_FILENAME).read_bytes()
            ).runtime.playlists
        )
        assert json.loads((root / V11_OPERATIONAL_FILENAME).read_text())["owner_revision"] == 2
        assert (root / "managed-state.json").read_bytes() == legacy_bytes
        assert not client.calls


def test_owner_source_disable_rejects_last_live_source_without_write(tmp_path: Path) -> None:
    root, _preview = _root(tmp_path, 10)
    with _server(root) as (server, client, _unused):
        store = open_managed_state_store(root)
        first = store.catalog.sources[0]
        state = store.load_readonly()
        store.save(
            replace(
                state,
                playlists=(
                    replace(state.playlists[0], source_ids=(first.id,), enabled=True),
                    *(replace(playlist, enabled=False) for playlist in state.playlists[1:]),
                ),
            )
        )
        source_service = server.RequestHandlerClass.source_admin_service
        managed = server.RequestHandlerClass.managed_admin_service
        assert source_service is not None and managed is not None
        source_service.store = managed.store = store
        source_service.catalog = managed.catalog = store.catalog
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        assert b"Cannot disable" in page
        before = (root / V11_OPERATIONAL_FILENAME).read_bytes()
        status, _, _ = _request(
            server,
            "POST",
            "/admin/sources/set-enabled",
            cookie=cookie,
            form={
                "csrf_token": _csrf(page),
                "source_id": str(first.id),
                "enabled": "0",
            },
        )
        assert status == 409
        assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == before
        assert not client.calls


def test_legacy_image_catalog_still_rejects_disabled_source(tmp_path: Path) -> None:
    root, _preview = _root(tmp_path, 10)
    first = open_managed_state_store(root).catalog.sources[0]
    with pytest.raises(ValueError, match="must be enabled"):
        BuiltInCatalog((replace(first, enabled=False),), ())


def test_owner_source_mode_edit_reconciles_legacy_override_in_one_commit(
    tmp_path: Path,
) -> None:
    root, _preview = _root(tmp_path, 10)
    legacy_before = (root / "managed-state.json").read_bytes()
    with _server(root) as (server, client, _unused):
        store = open_managed_state_store(root)
        source = store.catalog.sources[0]
        effective_mode = next(
            mode for mode in PublicReleaseMode if mode != source.public_release_mode
        )
        state = store.load_readonly()
        store.save(
            replace(
                state,
                source_overrides=(
                    *tuple(item for item in state.source_overrides if item.source_id != source.id),
                    ManagedSourceOverride(source.id, effective_mode),
                ),
            )
        )
        service = server.RequestHandlerClass.source_admin_service
        managed = server.RequestHandlerClass.managed_admin_service
        assert service is not None and managed is not None
        service.store = managed.store = store
        service.catalog = managed.catalog = store.catalog
        assert service.get_owned_source(str(source.id)).public_release_mode == effective_mode
        updated, _ = service.update_metadata(
            str(source.id),
            display_name="Owner mode reconciled",
            origin_kind=source.origin.kind.value,
            origin_code=source.origin.code,
            language=str(source.languages[0]),
            editorial_scope=source.editorial_scope.value,
            # Omitted mode must preserve the effective override, not raw owner default.
            public_release_mode=None,
        )
        assert updated.public_release_mode == effective_mode
        assert not client.calls
        reopened = open_managed_state_store(root)
        assert reopened.catalog.sources[0].public_release_mode == effective_mode
        assert all(item.source_id != source.id for item in reopened.load().source_overrides)
        actual = parse_v11_operational((root / V11_OPERATIONAL_FILENAME).read_bytes())
        assert actual.runtime.sources[0].public_release_mode == effective_mode
        assert json.loads((root / V11_OPERATIONAL_FILENAME).read_text())["owner_revision"] == 1
        assert (root / "managed-state.json").read_bytes() == legacy_before


def test_legacy_custom_is_owner_managed_after_direct_migration(tmp_path: Path) -> None:
    """Historical owner overlay is promoted without losing special capabilities."""
    from news_bulletin_playlist.managed_state import ManagedStateStore

    root, _ = _root(tmp_path, 10)
    old = ManagedStateStore(root / "managed-state.json")
    legacy = old.load_readonly()
    installed = _installed()
    old.save(replace(legacy, sources=(installed,)))
    original_bytes = old.path.read_bytes()
    store = open_managed_state_store(root)
    owned = parse_v11_operational((root / V11_OPERATIONAL_FILENAME).read_bytes())
    assert owned.owner_sources_complete
    assert not owned.managed_overlay.sources
    assert installed.id in {source.id for source in store.catalog.sources}
    assert next(s for s in store.catalog.sources if s.id == installed.id) == source_definition(
        installed
    )
    assert owned.runtime.sources[-1] == source_definition(installed)
    assert old.path.read_bytes() == original_bytes


def test_v11_source_json_import_installs_owner_records_not_overlay(tmp_path: Path) -> None:
    root, legacy = _root(tmp_path, 10)
    store = open_managed_state_store(root)
    baseline_order = tuple(source.id for source in store.catalog.sources)
    record = _installed()
    document = export_sources((source_definition(record),))
    service = SourceImportService(
        store,
        ConfigurationSynchronization(),
        ProvisioningJournal(root / "managed-playlist-provisioning.json"),
    )
    plan = service.preview(document)
    assert plan.creates == 1
    result = service.apply(document, plan.fingerprint, keep_local=frozenset(), confirm_v8=True)
    assert result.created == 1
    assert not store.load_readonly().sources
    assert tuple(s.id for s in store.catalog.sources[: len(baseline_order)]) == baseline_order
    assert len(store.catalog.sources) == len(baseline_order) + 1
    new_id = result.rows[0].local_id
    assert new_id is not None
    assert store.catalog.sources[-1] == replace(source_definition(record), id=new_id)
    persisted = parse_v11_operational((root / V11_OPERATIONAL_FILENAME).read_bytes())
    assert new_id in persisted.owner_inventory.source_order.ids
    assert persisted.runtime.sources == effective_sources(store.catalog, store.load_readonly())
    reopened = open_managed_state_store(root)
    assert reopened.catalog == store.catalog
    assert service.preview(document).creates == 0
    assert len(legacy.original_runtime.playlists) == 4


def test_v11_manual_intake_uses_same_owner_catalog(tmp_path: Path) -> None:
    root, _ = _root(tmp_path, 10)
    with _server(root) as (server, client, _unused):
        store = open_managed_state_store(root)
        service = server.RequestHandlerClass.source_admin_service
        assert service is not None
        service.store = store
        service.catalog = store.catalog
        service.inspect = lambda *_args, **_kwargs: None  # isolate provider I/O
        source = _source()
        service.create(source)
        assert not client.calls
        assert not store.load_readonly().sources
        owned = parse_v11_operational((root / V11_OPERATIONAL_FILENAME).read_bytes())
        assert source.id in owned.owner_inventory.source_order.ids
        created = next(item for item in store.catalog.sources if item.id == source.id)
        assert created == source_definition(source)


def test_imported_owner_source_editor_is_available_on_first_get_and_provenance_is_custom(
    tmp_path: Path,
) -> None:
    root, _legacy = _root(tmp_path, 10)
    with _server(root) as (server, client, _original):
        owner_store = open_managed_state_store(root)
        admin = server.RequestHandlerClass.managed_admin_service
        source_admin = server.RequestHandlerClass.source_admin_service
        assert admin is not None and source_admin is not None
        admin.store = source_admin.store = owner_store
        admin.catalog = source_admin.catalog = owner_store.catalog
        imported = _installed()
        document = export_sources((source_definition(imported),))
        importer = SourceImportService(
            owner_store,
            ConfigurationSynchronization(),
            ProvisioningJournal(root / "managed-playlist-provisioning.json"),
        )
        preview = importer.preview(document)
        new_id = importer.apply(
            document, preview.fingerprint, keep_local=frozenset(), confirm_v8=False
        ).rows[0].local_id
        assert new_id is not None
        # The HTTP service still owns its pre-import catalog until snapshot().
        assert new_id not in {s.id for s in source_admin.catalog.sources}
        cookie = _login(server)
        status, _, editor = _request(
            server, "GET", f"/admin/sources/edit?source_id={new_id}", cookie=cookie
        )
        assert status == 200
        assert b"Edit installation source" in editor
        assert not client.calls
        exported = export_source_configuration(
            owner_store, ConfigurationSynchronization(), catalog=source_admin.catalog
        )
        records = {row.source.id: row for row in parse_sources(exported)}
        assert records[new_id].provenance == "custom"
        assert records[owner_store.catalog.sources[0].id].provenance == "built_in"
