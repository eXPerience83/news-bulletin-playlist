"""Historical source deletion requires complete owner and managed reference closure."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from _admin_http import csrf as _csrf
from _admin_http import login as _login
from _admin_http import request as _request
from _installed_fixtures import _installed
from _legacy_dataset import legacy_root as _root
from _source_admin_http_fixtures import _server

from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal
from news_bulletin_playlist.managed_state import (
    ManagedStateError,
    ManagedStateStore,
    source_definition,
)
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.portable_sources import export_sources
from news_bulletin_playlist.source_import import SourceImportService
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_codec import parse_v11_operational
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME


def test_owner_source_delete_rejects_historical_template_reference(tmp_path: Path) -> None:
    root, _preview = _root(tmp_path, 10)
    with _server(root) as (server, client, _unused):
        store = open_managed_state_store(root)
        managed = server.RequestHandlerClass.managed_admin_service
        source_service = server.RequestHandlerClass.source_admin_service
        assert managed is not None and source_service is not None
        managed.store = source_service.store = store
        managed.catalog = source_service.catalog = store.catalog
        referenced = store.catalog.playlists[0].default_source_ids[0]
        before = (root / V11_OPERATIONAL_FILENAME).read_bytes()
        cookie = _login(server)
        status, _, page = _request(
            server, "GET", f"/admin/sources/delete?source_id={referenced}", cookie=cookie
        )
        assert status == 200
        assert b"owner template" in page
        status, _, _ = _request(
            server,
            "POST",
            "/admin/sources/delete",
            cookie=cookie,
            form={"csrf_token": _csrf(page), "source_id": str(referenced)},
        )
        assert status == 409
        assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == before
        assert not client.calls


def test_owner_source_delete_unreferenced_keeps_order_and_other_parsers(tmp_path: Path) -> None:
    root, _preview = _root(tmp_path, 10)
    before_legacy = (root / "managed-state.json").read_bytes()
    with _server(root) as (server, client, _unused):
        store = open_managed_state_store(root)
        state = store.load_readonly()
        old_source_order = tuple(source.id for source in store.catalog.sources)
        deleted_id = old_source_order[0]
        untouched = {
            source.id: (source.parser_id, source.collection_filter_id, source.temporal_strategy)
            for source in store.catalog.sources
            if source.id != deleted_id
        }
        # Synthetic fixture only: release all destination/template references first.
        # Production Admin must require the operator to perform those removals.
        store.save(replace(state, playlists=(), templates=(), source_overrides=()))
        for template in tuple(store.catalog.playlists):
            store.delete_owner_template(template.id)
        managed = server.RequestHandlerClass.managed_admin_service
        source_service = server.RequestHandlerClass.source_admin_service
        assert managed is not None and source_service is not None
        managed.store = source_service.store = store
        managed.catalog = source_service.catalog = store.catalog
        cookie = _login(server)
        status, _, page = _request(
            server, "GET", f"/admin/sources/delete?source_id={deleted_id}", cookie=cookie
        )
        assert status == 200
        before_delete = (root / V11_OPERATIONAL_FILENAME).read_bytes()
        status, _, _ = _request(
            server,
            "POST",
            "/admin/sources/delete",
            cookie=cookie,
            form={"csrf_token": _csrf(page), "source_id": str(deleted_id)},
        )
        assert status == 303
        assert not client.calls
        assert (root / V11_OPERATIONAL_FILENAME).read_bytes() != before_delete
        reopened = open_managed_state_store(root)
        assert tuple(source.id for source in reopened.catalog.sources) == old_source_order[1:]
        for source in reopened.catalog.sources:
            assert (
                source.parser_id,
                source.collection_filter_id,
                source.temporal_strategy,
            ) == untouched[source.id]
        assert (root / "managed-state.json").read_bytes() == before_legacy


def test_legacy_installed_custom_source_deletion_clears_owner_and_overlay_together(
    tmp_path: Path,
) -> None:
    root, _ = _root(tmp_path, 10)
    original_store = ManagedStateStore(root / "managed-state.json")
    legacy = original_store.load_readonly()
    installed = _installed()
    original_store.save(replace(legacy, sources=(installed,)))
    immutable = original_store.path.read_bytes()
    with _server(root) as (server, client, _unused):
        store = open_managed_state_store(root)
        before = parse_v11_operational((root / V11_OPERATIONAL_FILENAME).read_bytes())
        assert installed.id in {item.id for item in before.owner_inventory.sources}
        assert installed.id not in {item.id for item in before.managed_overlay.sources}
        assert installed.id in {item.id for item in before.active_catalog.sources}
        managed = server.RequestHandlerClass.managed_admin_service
        sources = server.RequestHandlerClass.source_admin_service
        assert managed is not None and sources is not None
        managed.store = sources.store = store
        managed.catalog = sources.catalog = store.catalog
        cookie = _login(server)
        status, _, page = _request(
            server, "GET", f"/admin/sources/delete?source_id={installed.id}", cookie=cookie
        )
        assert status == 200
        status, _, _ = _request(
            server,
            "POST",
            "/admin/sources/delete",
            cookie=cookie,
            form={"csrf_token": _csrf(page), "source_id": str(installed.id)},
        )
        assert status == 303
        reopened = open_managed_state_store(root)
        current = parse_v11_operational((root / V11_OPERATIONAL_FILENAME).read_bytes())
        assert installed.id not in {item.id for item in current.owner_inventory.sources}
        assert installed.id not in {item.id for item in current.managed_overlay.sources}
        assert installed.id not in {item.id for item in reopened.load_readonly().sources}
        assert original_store.path.read_bytes() == immutable
        assert not client.calls


def test_owner_delete_failed_cas_never_clears_runtime_or_detector_journals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _root(tmp_path, 10)
    store = open_managed_state_store(root)
    current = store.load_readonly()
    store.save(replace(current, playlists=(), templates=(), source_overrides=()))
    for template in tuple(store.catalog.playlists):
        store.delete_owner_template(template.id)
    source_id = store.catalog.sources[0].id
    with _server(root) as (server, client, _unused):
        service = server.RequestHandlerClass.source_admin_service
        assert service is not None
        service.store = store
        service.catalog = store.catalog
        before = (root / V11_OPERATIONAL_FILENAME).read_bytes()

        def never_clear(_source_id: SourceId) -> None:
            pytest.fail("Failed owner CAS may not erase any configured source episode evidence")

        def fail_cas(_source_id: SourceId) -> None:
            raise ManagedStateError("synthetic stale CAS")

        assert service.first_journal is not None
        assert service.detector_journal is not None
        monkeypatch.setattr(service.first_journal, "clear_source_runtime_state", never_clear)
        monkeypatch.setattr(service.detector_journal, "clear_source_state", never_clear)
        monkeypatch.setattr(store, "delete_owner_source", fail_cas)
        with pytest.raises(ManagedStateError, match="synthetic stale CAS"):
            service.delete(str(source_id))
        assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == before
        assert source_id in {source.id for source in store.catalog.sources}
        assert not client.calls


def test_owner_delete_cleanup_failure_retires_id_before_reimport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    store = open_managed_state_store(root)
    original = replace(_installed(), id=SourceId("custom_" + "Z" * 22))
    store.install_owner_sources((original,))
    with _server(root) as (server, client, _unused):
        service = server.RequestHandlerClass.source_admin_service
        assert service is not None
        service.store = store
        service.catalog = store.catalog
        assert service.first_journal is not None

        def fail_after_owner_commit(_source_id: SourceId) -> None:
            raise OSError("synthetic interrupted SQLite cleanup")

        monkeypatch.setattr(
            service.first_journal, "clear_source_runtime_state", fail_after_owner_commit
        )
        with pytest.raises(OSError, match="synthetic interrupted SQLite cleanup"):
            service.delete(str(original.id))

        durable = parse_v11_operational((root / V11_OPERATIONAL_FILENAME).read_bytes())
        assert original.id not in durable.owner_inventory.source_order.ids
        assert durable.owner_inventory.retired_source_ids == (original.id,)
        assert original.id not in {s.id for s in store.catalog.sources}
        assert not client.calls

        document = export_sources((source_definition(original),))
        importer = SourceImportService(
            store,
            ConfigurationSynchronization(),
            ProvisioningJournal(root / "managed-playlist-provisioning.json"),
        )
        plan = importer.preview(document)
        assert plan.creates == 1
        assert plan.rows[0].local_id == SourceId(str(original.id) + "_2")
        result = importer.apply(
            document, plan.fingerprint, keep_local=frozenset(), confirm_v8=False
        )
        assert result.created == 1
        assert result.rows[0].local_id in {s.id for s in store.catalog.sources}
        assert original.id not in {s.id for s in store.catalog.sources}
        assert open_managed_state_store(root).load_readonly().sources == ()
        assert not client.calls


def test_retired_source_cleanup_retry_over_authenticated_admin_without_spotify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "retired"
    root.mkdir()
    store = open_managed_state_store(root)
    station = replace(_installed(), id=SourceId("custom_" + "Y" * 22))
    store.install_owner_sources((station,))
    with _server(root) as (server, client, _legacy):
        handler = server.RequestHandlerClass
        service = handler.source_admin_service
        managed = handler.managed_admin_service
        assert service is not None and managed is not None
        service.store = managed.store = store
        service.catalog = managed.catalog = store.catalog
        assert service.first_journal is not None and service.detector_journal is not None

        original_clear = service.detector_journal.clear_source_state
        cleared: list[SourceId] = []

        def interrupt_cleanup(_id: SourceId) -> None:
            raise OSError("simulated SQLite interruption")

        monkeypatch.setattr(service.detector_journal, "clear_source_state", interrupt_cleanup)
        with pytest.raises(OSError, match="simulated SQLite interruption"):
            service.delete(str(station.id))
        old_bytes = (root / V11_OPERATIONAL_FILENAME).read_bytes()
        old_state = store.load_readonly()
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        assert b"Retired source maintenance" in page
        assert str(station.id).encode() in page
        assert b"Retry local cleanup" in page

        def complete_cleanup(source_id: SourceId) -> None:
            cleared.append(source_id)
            original_clear(source_id)

        monkeypatch.setattr(service.detector_journal, "clear_source_state", complete_cleanup)
        status, _, _ = _request(
            server, "POST", "/admin/sources/retired/cleanup", cookie=cookie,
            form={"csrf_token": _csrf(page), "source_id": str(station.id)}
        )
        assert status == 303
        assert cleared == [station.id]
        assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == old_bytes
        assert store.load_readonly() == old_state
        assert not client.calls

        status, _, fresh = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        # Cleanup is safe to repeat and must never release the retired ID.
        status, _, _ = _request(
            server, "POST", "/admin/sources/retired/cleanup", cookie=cookie,
            form={"csrf_token": _csrf(fresh), "source_id": str(station.id)}
        )
        assert status == 303
        assert cleared == [station.id, station.id]
        assert station.id in parse_v11_operational(
            (root / V11_OPERATIONAL_FILENAME).read_bytes()
        ).owner_inventory.retired_source_ids
        assert not client.calls

        status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        status, _, _ = _request(
            server, "POST", "/admin/sources/retired/cleanup", cookie=cookie,
            form={"csrf_token": _csrf(page), "source_id": "ser"}
        )
        assert status == 409
        assert cleared == [station.id, station.id]

        handler.inspection_hold = True
        try:
            held = _request(server, "GET", "/admin/sources", cookie=cookie)[2]
            assert b"Cleanup locked during inspection hold" in held
            assert b"Retry local cleanup" not in held
            status, _, _ = _request(
                server, "POST", "/admin/sources/retired/cleanup", cookie=cookie,
                form={"csrf_token": _csrf(held), "source_id": str(station.id)}
            )
            assert status == 423
        finally:
            handler.inspection_hold = False
        assert not client.calls
