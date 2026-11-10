"""Historical source deletion requires complete owner and managed reference closure."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from _admin_http import csrf as _csrf
from _admin_http import login as _login
from _admin_http import request as _request
from _legacy_dataset import legacy_root as _root
from _source_admin_http_fixtures import _server

from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
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
