"""Owner inventory portability without installing or contacting a provider."""

import hashlib
import json
from dataclasses import replace

import pytest
from _admin_effect_guards import forbid_effects as _forbid_effects
from _admin_http import login as _login
from _admin_http import request as _request
from _file_snapshots import snapshot_files as _files
from _legacy_dataset import legacy_root
from _source_admin_http_fixtures import _server

from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_state import ManagedSourceOverride
from news_bulletin_playlist.models import PublicReleaseMode
from news_bulletin_playlist.owner_inventory_export import (
    OwnerExportError,
    build_owner_inventory,
    export_owner_inventory,
    inspect_owner_inventory,
)
from news_bulletin_playlist.portable_playlists import PlaylistExportError, PortableCoverLoader
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_storage import V11OperationalStore

EXPORT = "/admin/playlists/owner-inventory.json"


def test_all_owner_templates_and_sources_round_trip(tmp_path):
    root, _ = legacy_root(tmp_path, 10)
    store = open_managed_state_store(root)
    owned = V11OperationalStore(root).load()
    used = {p.template_id for p in store.load_readonly().playlists}
    assert {t.id for t in owned.owner_inventory.templates.records} - used
    before = _files(root)
    sync = ConfigurationSynchronization()
    document = export_owner_inventory(root, sync)
    assert document == export_owner_inventory(root, sync)
    exported = inspect_owner_inventory(document)
    assert exported.inventory.templates == owned.owner_inventory.templates
    assert exported.inventory.source_order == owned.owner_inventory.source_order
    assert exported.inventory.source_order.definitions(exported.inventory.sources) == (
        owned.runtime.sources
    )
    assert exported.inventory.retired_source_ids == owned.owner_inventory.retired_source_ids
    covers = PortableCoverLoader(root)
    assert {a.cover_id for a in exported.artwork} == {
        t.cover_id for t in exported.inventory.templates.records
    }
    for artwork in exported.artwork:
        assert (artwork.jpeg, artwork.attribution) == covers.load(artwork.cover_id)
    for secret in (
        b"secret-oauth",
        b"secret-history",
        b"pending-post",
        b'"AAAAAAAAAAAAAAAAAAAAAA"',
    ):
        assert secret not in document
    assert before == _files(root)
    with pytest.raises(OwnerExportError):
        inspect_owner_inventory(document.replace(b"owner-inventory-export", b"invalid", 1))
    with pytest.raises(OwnerExportError):
        inspect_owner_inventory(document + b" ")


def _json_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def test_effective_source_override_and_tampered_artwork(tmp_path):
    root, _ = legacy_root(tmp_path, 10)
    store = open_managed_state_store(root)
    state = store.load_readonly()
    owned = V11OperationalStore(root).load()
    source = owned.runtime.sources[0]
    mode = (
        PublicReleaseMode.WALL_TIME
        if source.public_release_mode is PublicReleaseMode.INSTANT
        else PublicReleaseMode.INSTANT
    )
    store.save(replace(state, source_overrides=(ManagedSourceOverride(source.id, mode),)))
    before = _files(root)
    document = export_owner_inventory(root, ConfigurationSynchronization())
    result = inspect_owner_inventory(document)
    effective = result.inventory.source_order.definitions(result.inventory.sources)
    assert effective[0].public_release_mode is mode
    assert effective == V11OperationalStore(root).load().runtime.sources
    assert before == _files(root)

    root_object = json.loads(document)
    payload = root_object["payload"]
    payload["artwork"][0]["sha256"] = "0" * 64
    with pytest.raises(OwnerExportError, match="export digest"):
        inspect_owner_inventory(_json_bytes(root_object))
    root_object["sha256"] = hashlib.sha256(_json_bytes(payload)).hexdigest()
    with pytest.raises(OwnerExportError, match="artwork digest"):
        inspect_owner_inventory(_json_bytes(root_object))
    payload["artwork"].pop()
    root_object["sha256"] = hashlib.sha256(_json_bytes(payload)).hexdigest()
    with pytest.raises(OwnerExportError, match="artwork count"):
        inspect_owner_inventory(_json_bytes(root_object))


def test_empty_and_split_mode(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    open_managed_state_store(root)
    owned = V11OperationalStore(root).load()
    before = _files(root)
    document = export_owner_inventory(root, ConfigurationSynchronization())
    result = inspect_owner_inventory(document)
    assert result.inventory.sources == result.inventory.templates.records == result.artwork == ()
    with pytest.raises(OwnerExportError, match="split-mode"):
        build_owner_inventory(
            replace(owned, owner_sources_complete=False), PortableCoverLoader(root)
        )
    assert before == _files(root)


def test_missing_authority_does_not_initialize(tmp_path):
    before = _files(tmp_path)
    with pytest.raises(OwnerExportError, match="authority"):
        export_owner_inventory(tmp_path, ConfigurationSynchronization())
    assert before == _files(tmp_path)


def test_authenticated_held_export_and_late_artwork_failure(tmp_path, monkeypatch):
    root, _ = legacy_root(tmp_path, 10)
    open_managed_state_store(root)
    with _server(root) as (server, client, _legacy):
        handler = server.RequestHandlerClass
        assert _request(server, "GET", EXPORT)[0] in {303, 401, 403}
        cookie = _login(server)
        handler.managed_admin_service = None
        handler.inspection_hold = True
        before = _files(root)
        _forbid_effects(monkeypatch, handler.source_admin_service, server)
        status, headers, document = _request(server, "GET", EXPORT, cookie=cookie)
        assert status == 200, document
        assert headers["Content-Disposition"].endswith('owner-inventory-v1.json"')
        assert headers["Cache-Control"] == "no-store"
        assert inspect_owner_inventory(document).artwork
        assert _request(server, "GET", EXPORT + "?id=other", cookie=cookie)[0] == 400

        original = PortableCoverLoader.load
        seen = []

        def fail_second(loader, cover_id):
            seen.append(cover_id)
            if len(seen) == 2:
                raise PlaylistExportError("Missing cover")
            return original(loader, cover_id)

        monkeypatch.setattr(PortableCoverLoader, "load", fail_second)
        status, headers, _ = _request(server, "GET", EXPORT, cookie=cookie)
        assert status == 409
        assert len(seen) == 2
        assert "Content-Disposition" not in headers
        assert before == _files(root)
        assert not client.calls
