"""Historical template policies are editable in Admin without losing dormant exceptions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from _admin_http import csrf as _csrf
from _admin_http import login as _login
from _admin_http import request as _request
from _legacy_dataset import legacy_root as _root
from _legacy_materialization_fixtures import _catalog_with_duration_exception, _legacy_state
from _source_admin_http_fixtures import _server

from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.owner_template_records import OwnerTemplateSet
from news_bulletin_playlist.v11_managed_adapter import (
    ForwardOnlyManagedStateStore,
    open_managed_state_store,
)
from news_bulletin_playlist.v11_operational_storage import (
    V11_OPERATIONAL_FILENAME,
    V11OperationalStore,
)


def test_owner_template_admin_edit_persists_without_spotify_or_lost_sources(
    tmp_path: Path,
) -> None:
    root, preview = _root(tmp_path, 10)
    legacy_before = (root / "managed-state.json").read_bytes()
    with _server(root) as (server, client, _legacy_store):
        store = open_managed_state_store(root)
        service = server.RequestHandlerClass.managed_admin_service
        assert service is not None
        service.store = store
        service.catalog = store.catalog
        original = store.catalog.playlists[0]
        digest = hashlib.sha256(OwnerTemplateSet((original,)).serialize()).hexdigest()
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/", cookie=cookie)
        assert status == 200
        assert b"Installation-owned templates" in page
        assert b"/admin/playlists/templates/owner/update" in page

        form = {
            "csrf_token": _csrf(page),
            "template_id": str(original.id),
            "template_digest": digest,
            "display_name": "Revised owner template",
            "description": original.description.replace("\n", "\r\n"),
            "retention_hours": str(original.retention_hours + 1),
            "max_episodes": str(original.max_episodes),
            "min_duration_seconds": str(original.duration_policy.default_min_seconds),
            "max_duration_seconds": str(original.duration_policy.default_max_seconds),
        }
        status, _, _ = _request(
            server, "POST", "/admin/playlists/templates/owner/update", cookie=cookie, form=form
        )
        assert status == 303
        current = open_managed_state_store(root).catalog.playlists[0]
        assert current.display_name == "Revised owner template"
        assert current.description == original.description
        assert current.retention_hours == original.retention_hours + 1
        assert current.default_source_ids == original.default_source_ids
        assert current.duration_policy.exceptions == original.duration_policy.exceptions
        assert current.cover_id == original.cover_id
        assert not client.calls
        payload = (root / V11_OPERATIONAL_FILENAME).read_bytes()
        assert json.loads(payload)["owner_revision"] == 1
        status, _, refreshed = _request(server, "GET", "/admin/", cookie=cookie)
        assert status == 200
        form["csrf_token"] = _csrf(refreshed)
        status, _, _ = _request(
            server, "POST", "/admin/playlists/templates/owner/update", cookie=cookie, form=form
        )
        assert status == 409
        assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == payload
    assert (root / "managed-state.json").read_bytes() == legacy_before
    assert len(preview.original_runtime.playlists) == 4


def test_owner_template_edit_keeps_dormant_exception_after_reducing_max(tmp_path: Path):
    catalog = _catalog_with_duration_exception()
    root = tmp_path / "legacy"
    root.mkdir()
    ManagedStateStore(root / "managed-state.json", catalog=catalog).save(_legacy_state(10, catalog))
    V11OperationalStore(root).initialize_from_legacy(catalog=catalog)
    store = open_managed_state_store(root)
    assert isinstance(store, ForwardOnlyManagedStateStore)
    original = next(t for t in store.catalog.playlists if t.duration_policy.exceptions)
    exception = original.duration_policy.exceptions[0]
    assert exception.max_seconds >= 1800
    updated = replace(
        original,
        duration_policy=replace(original.duration_policy, default_max_seconds=1800),
    )
    store.save_owner_template(updated)
    reopened = open_managed_state_store(root)
    changed = next(t for t in reopened.catalog.playlists if t.id == original.id)
    assert changed.duration_policy.default_max_seconds == 1800
    assert changed.duration_policy.exceptions == original.duration_policy.exceptions
    assert exception.source_id in changed.default_source_ids
    assert changed.default_source_ids == original.default_source_ids


def test_owner_template_delete_rejects_referenced_destination_without_writes(
    tmp_path: Path,
) -> None:
    root, _preview = _root(tmp_path, 10)
    with _server(root) as (server, client, _legacy_store):
        store = open_managed_state_store(root)
        service = server.RequestHandlerClass.managed_admin_service
        assert service is not None
        service.store = store
        service.catalog = store.catalog
        state = store.load_readonly()
        target = next(
            t
            for t in store.catalog.playlists
            if any(p.template_id == t.id for p in state.playlists)
        )
        before = (root / V11_OPERATIONAL_FILENAME).read_bytes()
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/", cookie=cookie)
        assert status == 200
        digest = hashlib.sha256(OwnerTemplateSet((target,)).serialize()).hexdigest()
        status, _, _ = _request(
            server,
            "POST",
            "/admin/playlists/templates/owner/delete",
            cookie=cookie,
            form={
                "csrf_token": _csrf(page),
                "template_id": str(target.id),
                "template_digest": digest,
                "confirm_owner_delete": "1",
            },
        )
        assert status == 409
        assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == before
        assert not client.calls
        assert target.id in {t.id for t in open_managed_state_store(root).catalog.playlists}


def test_owner_template_delete_unused_is_durable_and_cas_guarded(
    tmp_path: Path,
) -> None:
    root, preview = _root(tmp_path, 10)
    before_legacy = (root / "managed-state.json").read_bytes()
    with _server(root) as (server, client, _legacy_store):
        store = open_managed_state_store(root)
        service = server.RequestHandlerClass.managed_admin_service
        assert service is not None
        service.store = store
        service.catalog = store.catalog
        state = store.load_readonly()
        chosen = store.catalog.playlists[0]
        # Synthetic-only setup: release the template's managed reference before
        # deletion. A real operator must stop management explicitly.
        if any(p.template_id == chosen.id for p in state.playlists):
            store.save(
                replace(
                    state,
                    playlists=tuple(p for p in state.playlists if p.template_id != chosen.id),
                )
            )
        source_ids = tuple(source.id for source in store.catalog.sources)
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/", cookie=cookie)
        assert status == 200
        digest = hashlib.sha256(OwnerTemplateSet((chosen,)).serialize()).hexdigest()
        assert b"/admin/playlists/templates/owner/delete" in page
        original_v11 = (root / V11_OPERATIONAL_FILENAME).read_bytes()
        status, _, _ = _request(
            server,
            "POST",
            "/admin/playlists/templates/owner/delete",
            cookie=cookie,
            form={
                "csrf_token": _csrf(page),
                "template_id": str(chosen.id),
                "template_digest": digest,
                "confirm_owner_delete": "1",
            },
        )
        assert status == 303
        assert not client.calls
        after = (root / V11_OPERATIONAL_FILENAME).read_bytes()
        assert after != original_v11
        assert json.loads(after)["owner_revision"] == 1
        reopened = open_managed_state_store(root)
        assert chosen.id not in {t.id for t in reopened.catalog.playlists}
        assert tuple(source.id for source in reopened.catalog.sources) == source_ids
        assert (root / "managed-state.json").read_bytes() == before_legacy
        assert len(preview.original_runtime.playlists) == 4
        stale = open_managed_state_store(root)
        stale._expected_bytes = original_v11
        from news_bulletin_playlist.managed_state import ManagedStateError

        with pytest.raises(ManagedStateError, match="could not be deleted"):
            stale.delete_owner_template(chosen.id)
        assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == after
