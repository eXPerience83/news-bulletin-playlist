"""Schema-v11 is standalone, strict and read-only until a future transaction."""

from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path

import pytest
from test_legacy_catalog_snapshot import _root
from test_legacy_materialization import _catalog_with_duration_exception, _legacy_state

from news_bulletin_playlist.legacy_catalog_snapshot import (
    build_legacy_snapshot_v2,
)
from news_bulletin_playlist.legacy_materialization import preview_legacy_materialization
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.v11_candidate import build_v11_candidate
from news_bulletin_playlist.v11_operational_codec import (
    OPERATIONAL_SCHEMA_VERSION,
    V11OperationalError,
    build_v11_operational_from_candidate,
    parse_v11_operational,
)


@pytest.fixture(autouse=True)
def no_external_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("v11 schema codec attempted network or SQLite I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def _operational(root: Path, preview: object) -> bytes:
    original = (root / "managed-state.json").read_bytes()
    return build_v11_operational_from_candidate(
        build_v11_candidate(build_legacy_snapshot_v2(preview, original), original)
    )


@pytest.mark.parametrize("version", [6, 7, 8, 9, 10])
def test_standalone_v11_recreates_ordered_runtime_without_original_files(
    tmp_path: Path, version: int
) -> None:
    root, preview = _root(tmp_path, version)
    before = _files(root)
    encoded = _operational(root, preview)
    assert _operational(root, preview) == encoded
    result = parse_v11_operational(encoded)
    assert result.schema_version == OPERATIONAL_SCHEMA_VERSION
    assert result.runtime == preview.original_runtime
    assert result.managed_overlay == preview.original_state
    assert tuple(s.id for s in result.installed_sources) == tuple(
        source.id for source in preview.original_runtime.sources
    )
    assert len(result.playlist_examples) == 7
    assert len(result.managed_overlay.playlists) == 4
    assert result.is_live_migration_authorized is False
    assert _files(root) == before

    # The schema-v11 read path must NOT require any original state file, or
    # derive definitions from the current release's image-owned catalog.
    (root / "managed-state.json").unlink()
    (root / "oauth-private").unlink()
    assert parse_v11_operational(encoded) == result


def test_custom_drafts_disabled_selections_quarantine_and_dst_fold(tmp_path: Path) -> None:
    catalog = _catalog_with_duration_exception()
    root = tmp_path / "legacy"
    root.mkdir()
    state = _legacy_state(10, catalog)
    store = ManagedStateStore(root / "managed-state.json", catalog=catalog)
    store.save(state)
    before = _files(root)
    preview = preview_legacy_materialization(catalog, store.load_readonly())
    parsed = parse_v11_operational(_operational(root, preview))
    assert parsed.runtime == preview.original_runtime
    assert parsed.managed_overlay == state
    assert parsed.managed_overlay.source_overrides == state.source_overrides
    assert parsed.managed_overlay.templates == state.templates
    assert parsed.managed_overlay.quarantined_templates == state.quarantined_templates
    assert parsed.managed_overlay.playlists[-1].source_ids == state.playlists[-1].source_ids
    spain = next(x for x in parsed.playlist_examples if str(x.id) == "spain_spanish_news")
    assert spain.duration_policy.exceptions[0].edition_local_time.fold == 1
    assert _files(root) == before


@pytest.mark.parametrize(
    "change",
    [
        lambda doc: doc.update(schema_version=True),
        lambda doc: doc.update(schema_version=12),
        lambda doc: doc.update(extraneous="field"),
        lambda doc: doc.update(migration_origin_sha256="0" * 64),
        lambda doc: doc["installed_catalog"].update(source_order=[]),
        lambda doc: doc["installed_catalog"].update(schema_version=1),
        lambda doc: doc["managed_overlay"].update(playlists=[]),
        lambda doc: doc["managed_overlay"].update(unknown=True),
    ],
)
def test_mutated_v11_document_is_rejected_without_file_writes(
    tmp_path: Path, change
) -> None:
    root, preview = _root(tmp_path)
    before = _files(root)
    document = json.loads(_operational(root, preview))
    change(document)
    with pytest.raises(V11OperationalError):
        parse_v11_operational((json.dumps(document, sort_keys=True, indent=2) + "\n").encode())
    assert _files(root) == before


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"[]",
        b"not-json",
        b'{"format": "a", "format": "b"}',
        b'{"schema_version": NaN}',
    ],
)
def test_bad_operational_json_fails_closed(payload: bytes) -> None:
    with pytest.raises(V11OperationalError):
        parse_v11_operational(payload)


def test_noncanonical_and_oversized_v11_are_rejected(tmp_path: Path) -> None:
    root, preview = _root(tmp_path)
    encoded = _operational(root, preview)
    with pytest.raises(V11OperationalError, match="noncanonical"):
        parse_v11_operational(encoded + b" ")
    with pytest.raises(V11OperationalError, match="size"):
        parse_v11_operational(b"x" * (12 * 1024 * 1024 + 1))
