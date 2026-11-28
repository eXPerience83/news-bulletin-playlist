"""Offline normalization preserves all configuration and never touches providers."""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import replace

import pytest
from _file_snapshots import snapshot_files as _files
from _legacy_materialization_fixtures import _catalog_with_duration_exception, _legacy_state

from news_bulletin_playlist.cli import main
from news_bulletin_playlist.inspection_fence import startup_gate
from news_bulletin_playlist.managed_provisioning import MANAGED_PROVISIONING_FILENAME
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.overlay_normalization import (
    NORMALIZATION_CONFIRMATION,
    OverlayNormalizationError,
    normalize_offline,
)
from news_bulletin_playlist.v11_operational_codec import (
    normalize_v11_overlay,
    parse_v11_operational,
)
from news_bulletin_playlist.v11_operational_storage import (
    V11_AUTHORITY_FILENAME,
    V11_OPERATIONAL_FILENAME,
    V11OperationalStore,
)


def _migrated(root, schema):
    catalog = _catalog_with_duration_exception()
    ManagedStateStore(root / "managed-state.json", catalog=catalog).save(
        _legacy_state(schema, catalog)
    )
    (root / "oauth-private").write_bytes(b"untouched-token")
    (root / "history.sqlite3").write_bytes(b"untouched-history")
    store = V11OperationalStore(root)
    store.initialize_from_legacy(catalog=catalog)
    return store


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _normalize(root, raw):
    return normalize_offline(root, expected_sha256=_sha(raw), confirm=NORMALIZATION_CONFIRMATION)


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_offline_normalization_is_lossless_and_idempotent(tmp_path, schema, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Normalization attempted provider I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    store = _migrated(tmp_path, schema)
    raw = store.load_bytes()
    before = _files(tmp_path)
    original = parse_v11_operational(raw)
    candidate = normalize_v11_overlay(raw)
    assert _files(tmp_path) == before
    result = _normalize(tmp_path, raw)
    assert result["changed"] is True
    assert result["previous_schema"] == schema
    assert result["schema_version"] == 11
    assert store.load_bytes() == candidate
    assert result["state_sha256"] == _sha(candidate)
    new = V11OperationalStore(tmp_path).load()
    assert new.runtime == original.runtime
    assert new.managed_overlay == replace(original.managed_overlay, schema_version=11)
    assert new.owner_inventory == original.owner_inventory
    assert new.migration_origin_sha256 == original.migration_origin_sha256
    old_doc = json.loads(raw)
    new_doc = json.loads(candidate)
    old_overlay = old_doc.pop("managed_overlay")
    new_overlay = new_doc.pop("managed_overlay")
    assert old_doc == new_doc
    assert new_overlay.get("quarantined_templates") == old_overlay.get("quarantined_templates")
    assert [p["portable_id"] for p in new_overlay["playlists"]] == [
        p.get("portable_id") for p in old_overlay["playlists"]
    ]
    assert all(p["ordering_override"] is None for p in new_overlay["playlists"])
    assert all(t["ordering_override"] is None for t in new_overlay["templates"])
    after = _files(tmp_path)
    assert {k: v for k, v in after.items() if k != V11_OPERATIONAL_FILENAME} == {
        k: v for k, v in before.items() if k != V11_OPERATIONAL_FILENAME
    }
    assert _normalize(tmp_path, candidate)["changed"] is False
    assert _files(tmp_path) == after


@pytest.mark.parametrize("obstacle", ["journal", "dangling_symlink", "held", "marker", "stale"])
def test_normalization_refuses_unsafe_inputs_without_writes(tmp_path, obstacle):
    store = _migrated(tmp_path, 10)
    raw = store.load_bytes()
    journal = tmp_path / MANAGED_PROVISIONING_FILENAME
    if obstacle == "journal":
        journal.write_bytes(b"unresolved")
    elif obstacle == "dangling_symlink":
        journal.symlink_to(tmp_path / "missing")
    elif obstacle == "held":
        startup_gate(tmp_path, requested=True)
    elif obstacle == "marker":
        (tmp_path / V11_AUTHORITY_FILENAME).unlink()
    else:
        raw = b"stale"
    before = _files(tmp_path)
    with pytest.raises(OverlayNormalizationError):
        _normalize(tmp_path, raw)
    assert _files(tmp_path) == before
    if obstacle == "dangling_symlink":
        assert journal.is_symlink()
        assert journal.readlink() == tmp_path / "missing"


def test_confirmation_and_pre_v11_root_never_create_authority(tmp_path):
    before = _files(tmp_path)
    with pytest.raises(OverlayNormalizationError, match="confirmation"):
        normalize_offline(tmp_path, expected_sha256="0" * 64, confirm="")
    with pytest.raises(OverlayNormalizationError):
        _normalize(tmp_path, b"missing")
    assert _files(tmp_path) == before


def test_cli_native_v11_is_read_only_noop(tmp_path, capsys):
    store = V11OperationalStore(tmp_path)
    store.initialize_fresh()
    raw = store.load_bytes()
    before = _files(tmp_path)
    assert main(
        [
            "normalize-v11-overlay",
            "--data-dir",
            str(tmp_path),
            "--state-sha256",
            _sha(raw),
            "--confirm",
            NORMALIZATION_CONFIRMATION,
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "previous_schema": 11,
        "schema_version": 11,
        "previous_sha256": _sha(raw),
        "state_sha256": _sha(raw),
        "changed": False,
    }
    assert _files(tmp_path) == before
