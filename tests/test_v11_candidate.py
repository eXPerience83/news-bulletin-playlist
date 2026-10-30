"""Pure schema-v11 candidate equality, strict decoding and exact old-state rollback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_legacy_catalog_snapshot import _root
from test_legacy_materialization import _catalog_with_duration_exception, _legacy_state

from news_bulletin_playlist.legacy_catalog_snapshot import (
    build_legacy_snapshot,
    build_legacy_snapshot_v2,
)
from news_bulletin_playlist.legacy_materialization import preview_legacy_materialization
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.v11_candidate import (
    CANDIDATE_SCHEMA_VERSION,
    V11CandidateError,
    build_v11_candidate,
    parse_v11_candidate,
)


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_v11_candidate_is_lossless_reversible_and_readonly(tmp_path: Path, schema: int) -> None:
    root, preview = _root(tmp_path, schema)
    original = _file_bytes(root)
    legacy = original["managed-state.json"]
    frozen = build_legacy_snapshot_v2(preview, legacy)
    first = build_v11_candidate(frozen, legacy)
    assert build_v11_candidate(frozen, legacy) == first

    payload = json.loads(first)
    assert payload["schema_version"] == CANDIDATE_SCHEMA_VERSION
    ownership = payload["ownership"]
    assert len(ownership["installed_sources"]) == 13
    assert len(ownership["playlist_examples"]) == 7
    assert len(ownership["managed_destinations"]) == 4
    assert len(ownership["source_order"]) == 13
    assert b"secret-oauth" not in first
    assert b"secret-history" not in first
    assert b"pending-post" not in first

    decoded = parse_v11_candidate(first)
    assert decoded.original_managed_state_bytes == legacy
    assert decoded.frozen_catalog_v2_bytes == frozen
    assert decoded.projection.source_order_verified
    assert decoded.projection.projected_runtime == preview.original_runtime
    assert decoded.projection.managed_destinations == preview.original_state.playlists
    assert decoded.projection.original_state == preview.original_state
    assert build_v11_candidate(
        decoded.frozen_catalog_v2_bytes, decoded.original_managed_state_bytes
    ) == first
    assert _file_bytes(root) == original


def test_v11_candidate_retains_drafts_quarantine_dormant_sources_and_dst_fold(
    tmp_path: Path,
) -> None:
    catalog = _catalog_with_duration_exception()
    root = tmp_path / "legacy"
    root.mkdir()
    state = _legacy_state(10, catalog)
    store = ManagedStateStore(root / "managed-state.json", catalog=catalog)
    store.save(state)
    raw = store.path.read_bytes()
    preview = preview_legacy_materialization(catalog, store.load_readonly())
    frozen = build_legacy_snapshot_v2(preview, raw)
    original = _file_bytes(root)

    result = parse_v11_candidate(build_v11_candidate(frozen, raw))
    assert result.projection.original_state == preview.original_state
    assert result.projection.projected_runtime == preview.original_runtime
    assert len(result.projection.managed_destinations) == 4
    assert result.projection.local_drafts == preview.original_state.templates
    assert result.projection.quarantined_drafts == preview.original_state.quarantined_templates
    assert result.projection.managed_destinations[-1].source_ids == state.playlists[-1].source_ids
    noon = next(
        item
        for item in result.projection.playlist_examples
        if str(item.id) == "spain_spanish_news"
    )
    assert noon.duration_policy.exceptions[0].edition_local_time.fold == 1
    assert _file_bytes(root) == original


def test_old_unordered_v1_sidecar_cannot_be_used_as_v11_source(tmp_path: Path) -> None:
    root, preview = _root(tmp_path)
    legacy = (root / "managed-state.json").read_bytes()
    with pytest.raises(V11CandidateError, match="ordered frozen v2"):
        build_v11_candidate(build_legacy_snapshot(preview, legacy), legacy)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda obj: obj.update(schema_version=True),
        lambda obj: obj.update(schema_version=12),
        lambda obj: obj.update(unexpected=True),
        lambda obj: obj.update(legacy_managed_state_sha256="0" * 64),
        lambda obj: obj.update(frozen_catalog_v2_sha256="0" * 64),
        lambda obj: obj.update(original_state_base64="!!!"),
        lambda obj: obj["ownership"].update(source_order=[]),
        lambda obj: obj["ownership"].update(managed_destinations=[]),
        lambda obj: obj["ownership"].update(local_drafts=[{"id": "injected"}]),
        lambda obj: obj["ownership"].update(playlist_examples=[]),
    ],
)
def test_v11_candidate_rejects_mutated_sections(tmp_path: Path, tamper) -> None:
    root, preview = _root(tmp_path)
    original = _file_bytes(root)
    legacy = original["managed-state.json"]
    document = json.loads(build_v11_candidate(build_legacy_snapshot_v2(preview, legacy), legacy))
    tamper(document)
    with pytest.raises(V11CandidateError):
        parse_v11_candidate((json.dumps(document, sort_keys=True) + "\n").encode("utf-8"))
    assert _file_bytes(root) == original


@pytest.mark.parametrize(
    "raw",
    [
        b'{"format": "one", "format": "two"}',
        b'{"schema_version": NaN}',
        b"[]",
        b"",
        b"not JSON",
    ],
)
def test_invalid_candidate_documents_fail_closed(raw: bytes) -> None:
    with pytest.raises(V11CandidateError):
        parse_v11_candidate(raw)


def test_large_or_noncanonical_candidate_is_rejected(tmp_path: Path) -> None:
    root, preview = _root(tmp_path)
    raw = (root / "managed-state.json").read_bytes()
    payload = build_v11_candidate(build_legacy_snapshot_v2(preview, raw), raw)
    with pytest.raises(V11CandidateError):
        parse_v11_candidate(payload + b" ")
    with pytest.raises(V11CandidateError, match="size"):
        parse_v11_candidate(b"x" * (24 * 1024 * 1024 + 1))
