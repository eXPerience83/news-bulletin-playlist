"""Self-contained configuration exports and explicit identity persistence, without providers."""

from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import replace
from datetime import time
from zipfile import ZipFile

import pytest
from test_custom_playlist_templates import _draft
from test_installed_sources import _installed
from test_managed_sources import _state

from news_bulletin_playlist import managed_state, portable_playlists
from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.custom_cover_assets import CustomCoverAssetStore
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_state import (
    ManagedSourceOverride,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    QuarantinedPlaylistTemplate,
    activate_template,
    parse_managed_state,
    serialize_managed_state,
)
from news_bulletin_playlist.models import (
    DurationPolicy,
    DurationPolicyException,
    PlaylistId,
    PublicReleaseMode,
    SourceId,
)
from news_bulletin_playlist.portable_playlists import (
    PlaylistExportError,
    PlaylistExportService,
    PortableCoverLoader,
)
from news_bulletin_playlist.portable_sources import parse_sources

UUID = "b96ab2b3-6568-459c-a391-3a993280e174"
OTHER_UUID = "abb9730d-d6eb-4235-b9b4-1ea1c127d67c"


def _service(tmp_path, state=None, *, catalog=BUILTIN_CATALOG):
    store = ManagedStateStore(tmp_path / "managed-state.json", catalog=catalog)
    if state is not None:
        store.save(state)
    return PlaylistExportService(
        store, ConfigurationSynchronization(), PortableCoverLoader(tmp_path)
    )


def _ready(state=None):
    state = state or _state()
    return replace(
        state,
        schema_version=9,
        playlists=tuple(replace(p, portable_id=UUID) for p in state.playlists),
    )


def _unpack(payload):
    with ZipFile(io.BytesIO(payload)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
        assert archive.namelist()[:3] == ["manifest.json", "manifest.sha256", "sources.json"]
        assert len(files) == 4
        assert all(
            i.date_time == (1980, 1, 1, 0, 0, 0)
            and not i.extra
            and not i.comment
            and i.compress_type == 0
            for i in archive.infolist()
        )
    manifest = json.loads(files["manifest.json"])
    assert (
        files["manifest.sha256"]
        == (hashlib.sha256(files["manifest.json"]).hexdigest() + "\n").encode()
    )
    for path, expected in manifest["files"].items():
        assert expected == {
            "size": len(files[path]),
            "sha256": hashlib.sha256(files[path]).hexdigest(),
        }
    return manifest["playlist"], files


@pytest.mark.parametrize("template", BUILTIN_CATALOG.playlists, ids=lambda t: t.id)
def test_managed_bundles_are_deterministic_complete_and_private(tmp_path, template):
    playlist = replace(activate_template(template, "PRIVATE-DESTINATION"), portable_id=UUID)
    service = _service(tmp_path, ManagedState(schema_version=9, playlists=(playlist,)))
    original = service.store.path.read_bytes()
    identity, payload = service.export("managed", str(playlist.id))
    assert identity == UUID
    assert service.export("managed", str(playlist.id))[1] == payload
    config, files = _unpack(payload)
    assert config["source_ids"] == list(template.default_source_ids)
    assert config["countries"] == list(template.countries)
    assert config["languages"] == list(template.languages)
    assert config["ordering"] == template.ordering
    assert {p.source.id for p in parse_sources(files["sources.json"])} == set(
        template.default_source_ids
    )
    cover = config["cover"]
    expected = service.covers.roots[-1] / (playlist.cover_id + ".jpg")
    assert files[cover["path"]] == expected.read_bytes()
    assert b"PRIVATE-DESTINATION" not in payload and str(tmp_path).encode() not in payload
    assert service.store.path.read_bytes() == original


def test_all_thirteen_sources_and_dormant_assignments_override_installed(tmp_path):
    installed = _installed(enabled=False)
    playlist = replace(
        activate_template(BUILTIN_CATALOG.playlists[0], "PRIVATE"),
        portable_id=UUID,
        source_ids=tuple(s.id for s in BUILTIN_CATALOG.sources) + (installed.id,),
    )
    state = ManagedState(
        schema_version=9,
        playlists=(playlist,),
        sources=(installed,),
        source_overrides=(ManagedSourceOverride(SourceId("rne"), PublicReleaseMode.DISABLED),),
    )
    service = _service(tmp_path, state)
    config, files = _unpack(service.export("managed", str(playlist.id))[1])
    sources = {s.source.id: s.source for s in parse_sources(files["sources.json"])}
    assert len(sources) == 14
    assert config["source_ids"] == list(playlist.source_ids)
    assert sources[installed.id].enabled is False
    assert sources[installed.id].countries == installed.countries
    assert sources[installed.id].languages == installed.languages
    assert sources["rne"].public_release_mode is PublicReleaseMode.DISABLED
    assert (
        sources["ser"].parser_id == "ser"
        and sources["un_news_es"].collection_filter_id == "un_news_es_minutes"
    )


@pytest.mark.parametrize("cover_id", ["custom_default", "international_english_news", "uploaded"])
def test_draft_covers_and_exact_policy(tmp_path, cover_id):
    if cover_id == "uploaded":
        original = PortableCoverLoader(tmp_path).load("international_english_news")[0]
        cover_id = CustomCoverAssetStore(tmp_path / "custom-covers").save(original)
    draft = _draft(
        portable_id=UUID,
        cover_id=cover_id,
        min_duration_seconds=121,
        max_duration_seconds=907,
        hidden=True,
    )
    service = _service(tmp_path, ManagedState(schema_version=9, templates=(draft,)))
    config, files = _unpack(service.export("draft", str(draft.id))[1])
    assert config["configuration"] == {"enabled": None, "hidden": True}
    assert config["duration_policy"] == {"min_seconds": 121, "max_seconds": 907, "exceptions": []}
    assert config["retention_hours"] == 72
    assert files[config["cover"]["path"]] == service.covers.load(cover_id)[0]
    assert config["cover"]["attribution"]["license"] is None


def test_managed_legacy_values_are_not_editor_truncated(tmp_path):
    template = BUILTIN_CATALOG.playlists[0]
    exception = DurationPolicyException(
        "late", template.default_source_ids[0], time(14, 0, 31, 123456, fold=1), 123456
    )
    template = replace(
        template, duration_policy=DurationPolicy(default_max_seconds=1800, exceptions=(exception,))
    )
    catalog = BuiltInCatalog(BUILTIN_CATALOG.sources, (template,))
    playlist = replace(
        activate_template(template, "PRIVATE"),
        portable_id=UUID,
        display_name="Legacy" * 30,
        description="d" * 248,
        retention_hours=9999,
        max_episodes=10001,
        min_duration_seconds=123,
        max_duration_seconds=100001,
    )
    service = _service(
        tmp_path, ManagedState(schema_version=9, playlists=(playlist,)), catalog=catalog
    )
    config, _ = _unpack(service.export("managed", str(playlist.id))[1])
    assert config["display_name"] == playlist.display_name
    assert config["description"] == playlist.description
    assert config["retention_hours"] == 9999 and config["max_episodes"] == 10001
    assert config["duration_policy"]["max_seconds"] == 100001
    assert config["duration_policy"]["exceptions"] == [
        {
            "id": "late",
            "source_id": exception.source_id,
            "edition_local_time": "14:00:31.123456",
            "edition_fold": 1,
            "max_seconds": 123456,
        }
    ]


@pytest.mark.parametrize("schema", [6, 7, 8, 9])
def test_explicit_identity_preparation_no_read_migration(tmp_path, schema):
    state = replace(_state(), schema_version=schema)
    service = _service(tmp_path, state)
    original = service.store.path.read_bytes()
    fingerprint, version, identity = service.preparation("managed", str(state.playlists[0].id))
    assert version == schema and identity is None
    with pytest.raises(PlaylistExportError, match="Prepare"):
        service.export("managed", str(state.playlists[0].id))
    assert service.store.path.read_bytes() == original
    if schema < 9:
        with pytest.raises(PlaylistExportError, match="acknowledgement"):
            service.prepare(
                "managed", str(state.playlists[0].id), fingerprint, acknowledge_v9=False
            )
        assert service.store.path.read_bytes() == original
    identity = service.prepare(
        "managed", str(state.playlists[0].id), fingerprint, acknowledge_v9=True
    )
    prepared = service.store.load_readonly()
    assert prepared == replace(
        state, schema_version=9, playlists=(replace(state.playlists[0], portable_id=identity),)
    )
    new_fingerprint = service.preparation("managed", str(state.playlists[0].id))[0]
    current = service.store.path.read_bytes()
    assert (
        service.prepare(
            "managed", str(state.playlists[0].id), new_fingerprint, acknowledge_v9=False
        )
        == identity
    )
    assert service.store.path.read_bytes() == current


def test_preserve_quarantine_drafts_installed_and_distinct_identities(tmp_path):
    state = replace(
        _state(),
        schema_version=8,
        sources=(_state().sources[0], _installed()),
        templates=(_draft(),),
        quarantined_templates=(
            QuarantinedPlaylistTemplate(
                1,
                "invalid_definition",
                json.dumps({"id": "broken", "sentinel": "keep"}, sort_keys=True),
            ),
        ),
    )
    service = _service(tmp_path, state)
    identities = []
    for kind, id_ in [
        ("managed", str(state.playlists[0].id)),
        ("draft", str(state.templates[0].id)),
    ]:
        fp = service.preparation(kind, id_)[0]
        identities.append(service.prepare(kind, id_, fp, acknowledge_v9=True))
    loaded = service.store.load_readonly()
    assert len(set(identities)) == 2
    assert loaded.sources == state.sources
    assert loaded.quarantined_templates == state.quarantined_templates
    assert replace(loaded.playlists[0], portable_id=None) == state.playlists[0]
    assert replace(loaded.templates[0], portable_id=None) == state.templates[0]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-uuid",
        "B96AB2B3-6568-459C-A391-3A993280E174",
        "00000000-0000-0000-0000-000000000000",
    ],
)
def test_invalid_identity_rejected(tmp_path, bad):
    state = _ready()
    with pytest.raises(ManagedStateError):
        serialize_managed_state(
            replace(state, playlists=(replace(state.playlists[0], portable_id=bad),))
        )


def test_identity_schema_gating_duplicate_and_required_fields():
    state = _ready()
    for version in (6, 7, 8):
        with pytest.raises(ManagedStateError):
            serialize_managed_state(replace(state, schema_version=version))
    with pytest.raises(ManagedStateError, match="Duplicate"):
        serialize_managed_state(replace(state, templates=(_draft(portable_id=UUID),)))
    data = serialize_managed_state(state)
    del data["playlists"][0]["portable_id"]
    with pytest.raises(ManagedStateError):
        parse_managed_state(data)


@pytest.mark.parametrize("stage", ["fsync_file", "replace", "chmod", "fsync_directory"])
def test_identity_atomic_failures_readback_and_retry(tmp_path, monkeypatch, stage):
    service = _service(tmp_path, _state())
    id_ = str(service.store.load_readonly().playlists[0].id)
    fp = service.preparation("managed", id_)[0]
    original = service.store.path.read_bytes()

    def fail(*a, **kw):
        raise OSError("private path must not escape")

    with monkeypatch.context() as patch:
        if stage == "fsync_directory":
            patch.setattr(managed_state, "fsync_directory", fail)
        else:
            patch.setattr(
                os, {"fsync_file": "fsync", "replace": "replace", "chmod": "chmod"}[stage], fail
            )
        if stage in {"fsync_file", "replace"}:
            with pytest.raises(PlaylistExportError, match="unchanged"):
                service.prepare("managed", id_, fp, acknowledge_v9=True)
            assert service.store.path.read_bytes() == original
        else:
            identity = service.prepare("managed", id_, fp, acknowledge_v9=True)
            assert service.store.load_readonly().playlists[0].portable_id == identity
    assert not list(tmp_path.glob("*.tmp"))
    fp = service.preparation("managed", id_)[0]
    service.prepare("managed", id_, fp, acknowledge_v9=True)
    service.export("managed", id_)


def test_stale_identity_confirmation_and_installation_independence(tmp_path):
    left = tmp_path / "left"
    left.mkdir()
    right = tmp_path / "right"
    right.mkdir()
    a, b = _service(left, _state()), _service(right, _state())
    id_ = str(_state().playlists[0].id)
    fp = a.preparation("managed", id_)[0]
    changed = replace(_state(), playlists=(replace(_state().playlists[0], display_name="new"),))
    a.store.save(changed)
    with pytest.raises(PlaylistExportError, match="changed"):
        a.prepare("managed", id_, fp, acknowledge_v9=True)
    ids = [
        service.prepare("managed", id_, service.preparation("managed", id_)[0], acknowledge_v9=True)
        for service in (a, b)
    ]
    assert ids[0] != ids[1]


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink", "fifo", "oversized"])
def test_bundled_asset_fail_closed_no_partial_files(tmp_path, damage):
    state = _ready()
    service = _service(tmp_path, state)
    root = tmp_path / "assets"
    root.mkdir()
    path = root / (state.playlists[0].cover_id + ".jpg")
    if damage == "corrupt":
        path.write_bytes(b"\xff\xd8broken\xff\xd9")
    elif damage == "symlink":
        path.symlink_to(service.covers.roots[-1] / path.name)
    elif damage == "fifo":
        os.mkfifo(path)
    elif damage == "oversized":
        path.write_bytes(b"x" * (192 * 1024 + 1))
    service.covers = PortableCoverLoader(tmp_path, bundled_roots=(root,))
    original = service.store.path.read_bytes()
    with pytest.raises((PlaylistExportError, OSError)):
        service.export("managed", str(state.playlists[0].id))
    assert service.store.path.read_bytes() == original
    assert not list(tmp_path.glob("*.zip"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_ids", (SourceId("missing"),)),
        ("display_name", "x" * 4097),
        ("retention_hours", 2**63),
    ],
)
def test_lossy_or_missing_configuration_rejected(tmp_path, field, value):
    state = _ready()
    service = _service(tmp_path, state)
    # Inject a valid state snapshot; oversized values may not be accepted by the state loader.
    state = replace(state, playlists=(replace(state.playlists[0], **{field: value}),))
    with pytest.raises(PlaylistExportError):
        portable_playlists.playlist_bundle(
            state, service.store, "managed", str(state.playlists[0].id), service.covers
        )


def test_v9_new_drafts_edits_and_managed_recovery_keep_distinct_identities(tmp_path):
    from test_managed_provisioning import _request_intent

    from news_bulletin_playlist.managed_admin import ManagedAdminService

    store = ManagedStateStore(tmp_path / "managed-state.json")
    store.save(ManagedState(schema_version=9))
    service = ManagedAdminService(store)
    fields = dict(
        display_name="Draft",
        description="Exact",
        cover_id="custom_default",
        source_ids=("cnn", "un_news_en"),
        retention_hours=48,
        max_episodes=100,
        min_duration_minutes=1,
        max_duration_minutes=20,
    )
    draft = service.create_local_template(**fields)
    assert draft.portable_id
    updated = service.update_local_template(
        draft.id, **(fields | {"display_name": "Edited"}), hidden=False
    )
    assert updated.portable_id == draft.portable_id
    service.provisioning_journal.save(_request_intent().with_destination("SYNTHETIC"))
    managed = service.finalize_known_provisioning()
    assert managed.portable_id and managed.portable_id != draft.portable_id
    service.provisioning_journal.save(_request_intent().with_destination("SYNTHETIC"))
    assert service.finalize_known_provisioning().portable_id == managed.portable_id
    assert store.load_readonly().schema_version == 9


def test_source_import_preserves_v9_identities(tmp_path):
    from test_source_import import _apply, _document
    from test_source_import import _service as import_service

    service = import_service(tmp_path)
    service.store.save(
        replace(ManagedState(schema_version=9), templates=(_draft(portable_id=UUID),))
    )
    assert _apply(service, _document(), confirm_v8=False).created == 1
    state = service.store.load_readonly()
    assert state.schema_version == 9 and state.templates[0].portable_id == UUID


@pytest.mark.parametrize("limit", ["MAX_MANIFEST_BYTES", "MAX_PLAYLIST_ZIP_BYTES"])
def test_size_limits_fail_before_return(tmp_path, monkeypatch, limit):
    service = _service(tmp_path, _ready())
    original = service.store.path.read_bytes()
    monkeypatch.setattr(portable_playlists, limit, 1)
    with pytest.raises(PlaylistExportError, match="limit"):
        service.export("managed", str(_ready().playlists[0].id))
    assert service.store.path.read_bytes() == original


def test_missing_capability_registry_fails_closed(tmp_path, monkeypatch):
    from news_bulletin_playlist.registry import TITLE_PARSERS

    service = _service(tmp_path, _ready())
    # Snapshot is valid before registry drift; the portable codec still rejects absent capabilities.
    state = _ready()
    monkeypatch.delitem(TITLE_PARSERS, "release_date_title")
    with pytest.raises(ValueError):
        portable_playlists.playlist_bundle(
            state, service.store, "managed", str(state.playlists[0].id), service.covers
        )


def test_multilingual_draft_preserves_ordered_geography_and_tags(tmp_path):
    from news_bulletin_playlist.models import CountryCode, LanguageTag

    draft = _draft(
        portable_id=UUID,
        countries=(CountryCode("ES"), CountryCode("AR")),
        languages=(LanguageTag("es-ES"), LanguageTag("es-419"), LanguageTag("en")),
    )
    service = _service(tmp_path, ManagedState(schema_version=9, templates=(draft,)))
    config, _ = _unpack(service.export("draft", str(draft.id))[1])
    assert config["countries"] == ["ES", "AR"]
    assert config["languages"] == ["es-ES", "es-419", "en"]


def test_quarantined_dependency_blocks_only_affected_export(tmp_path):
    state = _ready()
    broken = replace(state.playlists[0], template_id=PlaylistId("custom_broken"))
    quarantine = QuarantinedPlaylistTemplate(
        0, "invalid_definition", json.dumps({"id": "custom_broken"}, sort_keys=True)
    )
    service = _service(
        tmp_path, replace(state, playlists=(broken,), quarantined_templates=(quarantine,))
    )
    with pytest.raises(PlaylistExportError, match="quarantined"):
        service.export("managed", str(broken.id))


def test_uploaded_fifo_is_rejected_without_blocking(tmp_path):
    from news_bulletin_playlist.custom_cover_assets import CustomCoverAssetError

    root = tmp_path / "custom-covers"
    root.mkdir(mode=0o700)
    id_ = "uploaded_" + "a" * 64
    os.mkfifo(root / (id_ + ".jpg"))
    with pytest.raises(CustomCoverAssetError, match="regular"):
        PortableCoverLoader(tmp_path).load(id_)
