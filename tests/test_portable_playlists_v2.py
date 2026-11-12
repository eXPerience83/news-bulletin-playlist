"""Offline ZIP v2 codec: complete historical template, dormant selectors and cover closure."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from datetime import time
from zipfile import ZipFile

import pytest
from _portable_playlist_fixtures import UUID, _service, rewrite

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.managed_state import ManagedState, activate_template
from news_bulletin_playlist.models import DurationPolicy, DurationPolicyException
from news_bulletin_playlist.playlist_bundle import (
    PlaylistBundleError,
    inspect_playlist_bundle,
    inspect_playlist_bundle_v2,
)
from news_bulletin_playlist.portable_playlists import PlaylistExportError
from news_bulletin_playlist.portable_sources import parse_sources


def _donor(tmp_path, *, different_cover: bool = False):
    original = BUILTIN_CATALOG.playlists[0]
    assert len(original.default_source_ids) >= 2
    first, dormant = original.default_source_ids[0], original.default_source_ids[-1]
    active_exception = DurationPolicyException(
        "active", first, time(2, 30, 12, 123456, fold=1), 3600
    )
    dormant_exception = DurationPolicyException(
        "dormant", dormant, time(14, 1, 31, 654321, fold=1), 2700
    )
    template = replace(
        original,
        duration_policy=DurationPolicy(
            default_max_seconds=1800,
            exceptions=(active_exception, dormant_exception),
        ),
    )
    catalog = BuiltInCatalog(BUILTIN_CATALOG.sources, (template,))
    playlist = replace(
        activate_template(template, "SECRET-REMOTE-DESTINATION"),
        portable_id=UUID,
        source_ids=(first,),
        max_duration_seconds=3000,
        cover_id=BUILTIN_CATALOG.playlists[1].cover_id if different_cover else template.cover_id,
    )
    store = _service(
        tmp_path,
        ManagedState(schema_version=9, playlists=(playlist,)),
        catalog=catalog,
    )
    return store, playlist, template, active_exception, dormant_exception


def test_v2_retains_full_dormant_template_and_selected_runtime(tmp_path):
    store, playlist, template, active, dormant = _donor(tmp_path)
    before = store.store.path.read_bytes()
    with pytest.raises(PlaylistExportError, match="ZIP v2"):
        store.export("managed", str(playlist.id))
    identity, data = store.export("managed", str(playlist.id), version=2)
    assert identity == UUID
    assert store.export("managed", str(playlist.id), version=2)[1] == data
    assert store.store.path.read_bytes() == before
    assert b"SECRET-REMOTE-DESTINATION" not in data
    with pytest.raises(PlaylistBundleError, match="ZIP envelope"):
        inspect_playlist_bundle(data)
    parsed = inspect_playlist_bundle_v2(data)
    assert parsed.version == 2
    assert parsed.template == template
    assert parsed.playlist["source_ids"] == list(playlist.source_ids)
    assert parsed.playlist["duration_policy"]["max_seconds"] == 3000
    assert parsed.exceptions == (active,)
    assert dormant not in parsed.exceptions
    sources = {item.source.id for item in parse_sources(parsed.source_document)}
    assert sources == set(template.default_source_ids)
    assert dormant.source_id in sources and dormant.edition_local_time.fold == 1
    assert dormant.edition_local_time.microsecond == 654321
    # Re-editing/reassigning later must not lose this dormant definition.
    assert dormant in parsed.template.duration_policy.exceptions


def test_v2_preserves_different_current_and_template_cover_assets(tmp_path):
    store, playlist, template, _active, _dormant = _donor(tmp_path, different_cover=True)
    assert playlist.cover_id != template.cover_id
    payload = store.export("managed", str(playlist.id), version=2)[1]
    parsed = inspect_playlist_bundle_v2(payload)
    with ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.namelist()
        assert len(entries) == 6
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == 2
        assert manifest["template"]["cover"]["original_id"] == template.cover_id
        assert manifest["playlist"]["cover"]["original_id"] == playlist.cover_id
        assert parsed.template_jpeg == archive.read(manifest["template"]["cover"]["path"])
        assert parsed.jpeg == archive.read(manifest["playlist"]["cover"]["path"])


@pytest.mark.parametrize(
    "mutation",
    [
        "template_sha",
        "missing_template",
        "unknown_template_key",
        "missing_source_closure",
        "active_exception_mismatch",
        "fold_mismatch",
        "cover_sha",
    ],
)
def test_v2_rejects_corrupt_or_incomplete_closure(tmp_path, mutation):
    store, playlist, _template, _active, _dormant = _donor(tmp_path)
    payload = store.export("managed", str(playlist.id), version=2)[1]
    before = store.store.path.read_bytes()

    def edit(manifest, data):
        if mutation == "template_sha":
            manifest["template"]["sha256"] = "0" * 64
        elif mutation == "missing_template":
            manifest.pop("template")
        elif mutation == "unknown_template_key":
            manifest["template"]["unknown"] = True
        elif mutation == "missing_source_closure":
            source_doc = json.loads(data["sources.json"])
            source_doc["sources"] = source_doc["sources"][:1]
            encoded = (
                json.dumps(source_doc, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
            ).encode()
            data["sources.json"] = encoded
            manifest["files"]["sources.json"] = {
                "size": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        elif mutation == "active_exception_mismatch":
            manifest["playlist"]["duration_policy"]["exceptions"] = []
        elif mutation == "fold_mismatch":
            manifest["playlist"]["duration_policy"]["exceptions"][0]["edition_fold"] = 0
        elif mutation == "cover_sha":
            manifest["template"]["cover"]["sha256"] = "0" * 64

    invalid = rewrite(payload, edit)
    with pytest.raises(PlaylistBundleError):
        inspect_playlist_bundle_v2(invalid)
    assert store.store.path.read_bytes() == before


def test_v1_v2_decoders_never_cross_accept(tmp_path):
    template = BUILTIN_CATALOG.playlists[0]
    playlist = replace(activate_template(template, "PRIVATE"), portable_id=UUID)
    store = _service(tmp_path, ManagedState(schema_version=9, playlists=(playlist,)))
    v1 = store.export("managed", str(playlist.id))[1]
    assert inspect_playlist_bundle(v1).version == 1
    with pytest.raises(PlaylistBundleError):
        inspect_playlist_bundle_v2(v1)
    with pytest.raises(PlaylistExportError):
        store.export("draft", str(playlist.id), version=2)
