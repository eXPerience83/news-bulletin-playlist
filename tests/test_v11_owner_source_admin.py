"""Admin owner inventory edits operate locally and retain historical processing capabilities."""

from __future__ import annotations

import json
from pathlib import Path

from _admin_http import csrf as _csrf
from _admin_http import login as _login
from _admin_http import request as _request
from _legacy_dataset import legacy_root as _root
from _source_admin_http_fixtures import _server

from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
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
