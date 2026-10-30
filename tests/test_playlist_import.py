"""Independent installation round trips, hostile archives and coordinated commit recovery."""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import sqlite3
import stat
from dataclasses import replace
from datetime import time
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest
from test_custom_playlist_templates import _draft
from test_installed_sources import EMPTY, _installed, _rich_state
from test_portable_playlists import OTHER_UUID, UUID, _ready
from test_portable_playlists import _service as export_service

from news_bulletin_playlist import managed_state
from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.custom_cover_assets import CustomCoverAssetError, CustomCoverAssetStore
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_admin import ManagedAdminError, ManagedAdminService
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedState,
    ManagedStateStore,
    activate_template,
    effective_sources,
    serialize_managed_state,
)
from news_bulletin_playlist.models import DurationPolicy, DurationPolicyException, SourceId
from news_bulletin_playlist.playlist_bundle import PlaylistBundleError, inspect_playlist_bundle
from news_bulletin_playlist.playlist_import import PlaylistImportError, PlaylistImportService
from news_bulletin_playlist.portable_playlists import PortableCoverLoader, canonical_json
from news_bulletin_playlist.portable_sources import PortableSourceError


@pytest.fixture(autouse=True)
def no_operational_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Playlist import attempted network/SQLite")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)


def donor(
    tmp_path, *, template=None, playlist=None, draft=None, catalog=BUILTIN_CATALOG, state=None
):
    root = tmp_path / "donor"
    root.mkdir(exist_ok=True)
    if state is None:
        if draft is not None:
            state = ManagedState(schema_version=9, templates=(draft,))
        else:
            template = template or catalog.playlists[0]
            playlist = playlist or replace(
                activate_template(template, "PRIVATE-DESTINATION"), portable_id=UUID
            )
            state = ManagedState(schema_version=9, playlists=(playlist,))
    service = export_service(root, state, catalog=catalog)
    kind = "draft" if draft is not None else "managed"
    id_ = str(draft.id if draft is not None else state.playlists[0].id)
    return service.export(kind, id_)[1]


def receiver(tmp_path, *, schema=6, catalog=EMPTY, state=None):
    root = tmp_path / "receiver"
    root.mkdir(exist_ok=True)
    store = ManagedStateStore(root / "managed-state.json", catalog=catalog)
    store.save(state or ManagedState(schema_version=schema))
    return PlaylistImportService(
        store,
        ConfigurationSynchronization(),
        ProvisioningJournal(root / "provisioning.json"),
        PortableCoverLoader(root),
    )


def apply(service, document, *, keep=None):
    preview = service.preview(document)
    return service.apply(
        document,
        preview.sources.fingerprint,
        keep_local=preview.sources.conflicts if keep is None else keep,
        confirm_schema=True,
    )


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def rewrite(document, edit=None, *, names=None, compression=0, duplicate=False, info_edit=None):
    with ZipFile(io.BytesIO(document)) as archive:
        entries = [(i, archive.read(i)) for i in archive.infolist()]
    data = {i.filename: b for i, b in entries}
    if edit:
        manifest = json.loads(data["manifest.json"])
        edit(manifest, data)
        data["manifest.json"] = canonical_json(manifest)
        data["manifest.sha256"] = (
            hashlib.sha256(data["manifest.json"]).hexdigest() + "\n"
        ).encode()
    output = io.BytesIO()
    with ZipFile(output, "w", compression=compression) as archive:
        for index, (old, _) in enumerate(entries):
            info = ZipInfo((names or {}).get(index, old.filename), date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = compression
            if info_edit:
                info_edit(index, info)
            archive.writestr(info, data[old.filename])
        if duplicate:
            archive.writestr(entries[0][0], data[entries[0][0].filename])
    return output.getvalue()


@pytest.mark.parametrize("template", BUILTIN_CATALOG.playlists, ids=lambda t: t.id)
def test_roundtrip_empty_independent_installation(tmp_path, template, monkeypatch):
    document = donor(tmp_path, template=template)
    service = receiver(tmp_path)
    before = files(service.store.path.parent)
    preview = service.preview(document)
    assert preview.status == "create" and not preview.blocked
    assert files(service.store.path.parent) == before
    saves = []
    real_save = service.store.save

    def save(state):
        saves.append(state)
        real_save(state)

    monkeypatch.setattr(service.store, "save", save)
    result = apply(service, document)
    assert result.created_draft and len(saves) == 1
    state = service.store.load_readonly()
    assert state.schema_version == 10 and not state.playlists
    assert state.templates[0].portable_id == UUID
    assert len(state.sources) == len(template.default_source_ids)
    assert all(isinstance(s, InstalledSource) for s in state.sources)
    assert (
        CustomCoverAssetStore(service.covers.store.root).load(state.templates[0].cover_id)
        == preview.bundle.jpeg
    )
    assert files(service.store.path.parent)["managed-state.json"] != before["managed-state.json"]
    # Restart and repeat: no state or artwork writes, even though IDs and cover ID changed.
    restarted = PlaylistImportService(
        service.store, ConfigurationSynchronization(), service.provisioning, service.covers
    )
    after = files(service.store.path.parent)
    assert not apply(restarted, document).created_draft
    assert len(saves) == 1 and files(service.store.path.parent) == after
    exported = export_service(service.store.path.parent, catalog=EMPTY)
    exported.store = service.store
    again = inspect_playlist_bundle(exported.export("draft", str(state.templates[0].id))[1])
    assert again.jpeg == preview.bundle.jpeg and again.playlist["portable_id"] == UUID
    assert not apply(
        restarted, exported.export("draft", str(state.templates[0].id))[1]
    ).created_draft


def test_all_thirteen_specialized_capabilities_disabled_assignments(tmp_path):
    template = BUILTIN_CATALOG.playlists[0]
    installed = _installed(enabled=False)
    p = replace(
        activate_template(template, "PRIVATE"),
        portable_id=UUID,
        source_ids=tuple(s.id for s in BUILTIN_CATALOG.sources) + (installed.id,),
    )
    document = donor(
        tmp_path, state=ManagedState(schema_version=9, playlists=(p,), sources=(installed,))
    )
    service = receiver(tmp_path)
    preview = service.preview(document)
    apply(service, document)
    state = service.store.load_readonly()
    imported = {s.external_references[0].external_id: s for s in effective_sources(EMPTY, state)}
    assert len(imported) == 14
    for record in preview.bundle.sources:
        definition = record.source
        assert (
            replace(imported[definition.external_references[0].external_id], id=definition.id)
            == definition
        )
    assert len(state.templates[0].default_source_ids) == 14
    assert any(not s.enabled for s in imported.values())


def test_legacy_policy_exceptions_multilingual_and_exact_editing(tmp_path):
    template = BUILTIN_CATALOG.playlists[0]
    exception = DurationPolicyException(
        "late", template.default_source_ids[0], time(14, 0, 13, 123456, fold=1), 120001
    )
    template = replace(template, duration_policy=DurationPolicy(1800, (exception,)))
    catalog = BuiltInCatalog(BUILTIN_CATALOG.sources, (template,))
    playlist = replace(
        activate_template(template, "PRIVATE"),
        portable_id=UUID,
        display_name="N" * 300,
        description="D" * 10000,
        retention_hours=9999,
        max_episodes=12000,
        min_duration_seconds=121,
        max_duration_seconds=100001,
    )
    document = donor(tmp_path, catalog=catalog, playlist=playlist)
    service = receiver(tmp_path)
    result = apply(service, document)
    draft = service.store.load_readonly().templates[0]
    assert draft.display_name == playlist.display_name and draft.description == playlist.description
    assert draft.retention_hours == 9999 and draft.max_episodes == 12000
    assert draft.min_duration_seconds == 121 and draft.max_duration_seconds == 100001
    assert draft.duration_exceptions[0].source_id == draft.default_source_ids[0]
    assert draft.duration_exceptions[0].edition_local_time == exception.edition_local_time
    admin = ManagedAdminService(
        service.store, catalog=EMPTY, cover_loader=service.covers.store.load
    )
    updated = admin.update_local_template(
        result.local_id,
        display_name=draft.display_name,
        description=draft.description,
        cover_id=draft.cover_id,
        source_ids=draft.default_source_ids,
        retention_hours=9999,
        max_episodes=12000,
        min_duration_seconds=122,
        max_duration_seconds=100002,
        hidden=False,
    )
    assert updated.duration_exceptions == draft.duration_exceptions and updated.portable_id == UUID
    assert service.store.load_readonly().templates[0].min_duration_seconds == 122
    with pytest.raises(ManagedAdminError, match="activation"):
        admin.activate(
            template_id=draft.id,
            display_name=draft.display_name,
            description=draft.description,
            cover_id=draft.cover_id,
            source_ids=draft.default_source_ids,
            access_token="unused",
        )


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_explicit_schema_adoption_and_readonly_compatibility(tmp_path, schema):
    service = receiver(tmp_path, schema=schema)
    document = donor(tmp_path)
    old = files(service.store.path.parent)
    preview = service.preview(document)
    assert files(service.store.path.parent) == old
    if schema < 10:
        with pytest.raises(PlaylistImportError, match="acknowledgement"):
            service.apply(
                document, preview.sources.fingerprint, keep_local=frozenset(), confirm_schema=False
            )
        assert files(service.store.path.parent) == old
    apply(service, document)
    assert service.store.load_readonly().schema_version == 10


def test_uuid_name_and_content_conflicts(tmp_path):
    document = donor(tmp_path)
    service = receiver(tmp_path)
    apply(service, document)
    changed = rewrite(document, lambda m, d: m["playlist"].update(description="different"))
    assert service.preview(changed).status == "uuid_conflict"
    before = files(service.store.path.parent)
    with pytest.raises(PlaylistImportError, match="UUID"):
        apply(service, changed)
    assert files(service.store.path.parent) == before
    other = rewrite(document, lambda m, d: m["playlist"].update(portable_id=OTHER_UUID))
    assert apply(service, other).created_draft
    assert len(service.store.load_readonly().templates) == 2


def test_managed_uuid_blocks_import(tmp_path):
    document = donor(tmp_path)
    state = _ready()
    service = receiver(tmp_path, catalog=BUILTIN_CATALOG, state=state)
    assert service.preview(document).status == "managed_uuid"
    before = files(service.store.path.parent)
    with pytest.raises(PlaylistImportError):
        apply(service, document)
    assert files(service.store.path.parent) == before


def test_same_show_different_local_id_and_keep_local(tmp_path):
    document = donor(tmp_path)
    definitions = inspect_playlist_bundle(document).sources
    installed = tuple(
        InstalledSource.from_definition(replace(r.source, id=SourceId("alias_" + str(i))))
        for i, r in enumerate(definitions)
    )
    service = receiver(tmp_path, state=ManagedState(schema_version=8, sources=installed))
    assert all(r.status == "reuse" for r in service.preview(document).sources.rows)
    apply(service, document)
    assert len(service.store.load_readonly().sources) == len(installed)
    state = service.store.load_readonly()
    modified = replace(state.sources[0], display_name="Local preference")
    service.store.save(replace(state, sources=(modified,) + state.sources[1:]))
    preview = service.preview(document)
    assert preview.sources.conflicts and preview.status == "identical"
    with pytest.raises(PlaylistImportError, match="explicit"):
        service.apply(
            document, preview.sources.fingerprint, keep_local=frozenset(), confirm_schema=True
        )
    before = files(service.store.path.parent)
    assert not apply(service, document).created_draft
    assert files(service.store.path.parent) == before


@pytest.mark.parametrize(
    "damage",
    [
        "truncated",
        "trailing",
        "prefix",
        "compressed",
        "traversal",
        "absolute",
        "duplicate",
        "extra",
        "symlink",
        "extra_metadata",
        "comment",
    ],
)
def test_hostile_zip_rejected_without_writes(tmp_path, damage):
    document = donor(tmp_path)
    if damage == "truncated":
        document = document[:-1]
    elif damage == "trailing":
        document += b"secret"
    elif damage == "prefix":
        document = b"prefix" + document
    elif damage == "compressed":
        document = rewrite(document, compression=ZIP_DEFLATED)
    elif damage == "traversal":
        document = rewrite(document, names={3: "../cover.jpg"})
    elif damage == "absolute":
        document = rewrite(document, names={3: "/tmp/cover.jpg"})
    elif damage == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            document = rewrite(document, duplicate=True)
    elif damage == "extra":
        document = rewrite(document, names={3: "unexpected.jpg"})
    elif damage == "symlink":
        document = rewrite(
            document,
            info_edit=lambda i, e: (
                setattr(e, "external_attr", (stat.S_IFLNK | 0o777) << 16) if i == 3 else None
            ),
        )
    elif damage == "extra_metadata":
        document = rewrite(
            document, info_edit=lambda i, e: setattr(e, "extra", b"\x99\x99\x00\x00")
        )
    elif damage == "comment":
        document = rewrite(document, info_edit=lambda i, e: setattr(e, "comment", b"secret"))
    service = receiver(tmp_path)
    before = files(service.store.path.parent)
    with pytest.raises(PlaylistBundleError):
        service.preview(document)
    assert files(service.store.path.parent) == before


@pytest.mark.parametrize(
    "field,value", [("version", True), ("version", 2), ("unknown", "x"), ("format", "unknown")]
)
def test_manifest_root_strict(tmp_path, field, value):
    document = rewrite(donor(tmp_path), lambda m, d: m.update({field: value}))
    with pytest.raises(PlaylistBundleError):
        inspect_playlist_bundle(document)


@pytest.mark.parametrize(
    "damage",
    [
        "hash",
        "size",
        "source_ref",
        "exception_ref",
        "uuid",
        "bad_image",
        "duplicate_keys",
        "missing_field",
        "unknown_field",
        "future_capability",
    ],
)
def test_content_validation_and_unsupported_preview(tmp_path, damage):
    def edit(m, d):
        p = m["playlist"]
        if damage == "hash":
            m["files"]["sources.json"]["sha256"] = "0" * 64
        elif damage == "size":
            p["cover"]["size"] = True
        elif damage == "source_ref":
            p["source_ids"] = ["missing"]
        elif damage == "exception_ref":
            p["duration_policy"]["exceptions"] = [
                {
                    "id": "e",
                    "source_id": "missing",
                    "edition_local_time": "12:00:00.000000",
                    "edition_fold": 0,
                    "max_seconds": 9000,
                }
            ]
        elif damage == "uuid":
            p["portable_id"] = "not-uuid"
        elif damage == "missing_field":
            p.pop("description")
        elif damage == "unknown_field":
            p["secret"] = "no"
        elif damage == "bad_image":
            d[p["cover"]["path"]] = b"\xff\xd8broken\xff\xd9"
        elif damage in {"duplicate_keys", "future_capability"}:
            data = json.loads(d["sources.json"])
            data["sources"][0]["processing"]["parser"]["version"] = 99
            raw = canonical_json(data)
            if damage == "duplicate_keys":
                raw = raw.replace(b'"version": 1', b'"version": 1, "version": 1')
            d["sources.json"] = raw
            m["files"]["sources.json"] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }

    document = rewrite(donor(tmp_path), edit)
    service = receiver(tmp_path)
    before = files(service.store.path.parent)
    if damage == "future_capability":
        preview = service.preview(document)
        assert preview.status == "unsupported" and preview.blocked
        with pytest.raises(PortableSourceError):
            apply(service, document)
    else:
        with pytest.raises(PlaylistBundleError):
            service.preview(document)
    assert files(service.store.path.parent) == before


@pytest.mark.parametrize("stage", ["replace", "file_fsync", "chmod", "directory_fsync"])
def test_atomic_state_faults_orphans_restart_and_retry(tmp_path, monkeypatch, stage):
    document = donor(tmp_path)
    service = receiver(tmp_path)
    before = service.store.path.read_bytes()

    def fail(*a, **kw):
        raise OSError("private details")

    real_save = service.store.save

    def save(candidate):
        with monkeypatch.context() as patch:
            if stage == "directory_fsync":
                patch.setattr(managed_state, "fsync_directory", fail)
            else:
                patch.setattr(
                    os, {"replace": "replace", "file_fsync": "fsync", "chmod": "chmod"}[stage], fail
                )
            real_save(candidate)

    monkeypatch.setattr(service.store, "save", save)
    if stage in {"replace", "file_fsync"}:
        with pytest.raises(PlaylistImportError, match="not committed"):
            apply(service, document)
        assert service.store.path.read_bytes() == before
        assert list(service.covers.store.root.glob("uploaded_*.jpg"))
    else:
        assert apply(service, document).recovered
        assert service.store.load_readonly().templates
    monkeypatch.setattr(service.store, "save", real_save)
    apply(service, document)
    assert len(service.store.load_readonly().templates) == 1
    assert len(list(service.covers.store.root.glob("uploaded_*.jpg"))) == 1


def test_state_and_pending_quarantine_preserved_stale_preview(tmp_path):
    from test_managed_provisioning import _request_intent

    state = _rich_state()
    service = receiver(tmp_path, catalog=BUILTIN_CATALOG, state=state)
    service.provisioning.save(_request_intent())
    document = donor(tmp_path)
    preview = service.preview(document)
    service.provisioning.save(_request_intent().with_destination("SYNTHETIC"))
    before = files(service.store.path.parent)
    with pytest.raises(PlaylistImportError, match="changed"):
        service.apply(
            document,
            preview.sources.fingerprint,
            keep_local=preview.sources.conflicts,
            confirm_schema=True,
        )
    assert files(service.store.path.parent) == before
    apply(service, document)
    after = service.store.load_readonly()
    assert (
        after.playlists == state.playlists
        and after.quarantined_templates == state.quarantined_templates
    )
    assert (
        after.source_overrides == state.source_overrides and state.templates[0] in after.templates
    )
    assert service.provisioning.path.read_bytes() == before["provisioning.json"]


@pytest.mark.parametrize(
    "source_id,title",
    [
        ("ser", "Las noticias de la SER, 12:00 (25/09/2026)"),
        ("rne", "NOTICIAS RNE – 25.09.2026 – 09.30H"),
        ("cnn", "CNN 5 cosas 09/25/2026 6:00 am"),
        ("rfi_fr", "Journal 25/09/2026 12h00 GMT"),
        ("rmf_fakty", "16:00 Fakty"),
        ("un_news_es", "La ONU en Minutos 25 de septiembre de 2026"),
    ],
)
def test_imported_runtime_keeps_chronology_and_filter_decisions(tmp_path, source_id, title):
    from datetime import UTC, datetime, timedelta

    from _spotify_catalogue import catalogue_page

    from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue

    original = next(s for s in BUILTIN_CATALOG.sources if s.id == source_id)
    template = replace(BUILTIN_CATALOG.playlists[0], default_source_ids=(original.id,))
    document = donor(tmp_path, template=template)
    service = receiver(tmp_path)
    apply(service, document)
    restored = effective_sources(EMPTY, service.store.load_readonly())[0]
    assert restored.id != original.id
    now = datetime(2026, 9, 25, 17, tzinfo=UTC)

    class Catalogue:
        def show_episodes(self, show_id, **kwargs):
            assert show_id == original.external_references[0].external_id
            titles = [title, "Entrevista especial"] if source_id == "un_news_es" else [title]
            return catalogue_page(
                [
                    {
                        "id": str(i) * 22,
                        "uri": "spotify:episode:" + str(i) * 22,
                        "name": name,
                        "release_date": "2026-09-25",
                        "release_date_precision": "day",
                        "duration_ms": 60000,
                    }
                    for i, name in enumerate(titles)
                ]
            )

    before = fetch_source_catalogue(Catalogue(), original, now=now, cutoff=now - timedelta(days=2))
    after = fetch_source_catalogue(Catalogue(), restored, now=now, cutoff=now - timedelta(days=2))
    assert before.episodes == tuple(replace(e, source_id=original.id) for e in after.episodes)
    assert before.filter_rejected_count == after.filter_rejected_count
    if source_id == "un_news_es":
        assert after.filter_rejected_count == 1


@pytest.mark.parametrize("cover", ["custom_default", "international_english_news", "uploaded"])
def test_draft_cover_bytes_and_hidden_observation(tmp_path, cover):
    draft = _draft(
        portable_id=UUID, hidden=True, cover_id="custom_default" if cover == "uploaded" else cover
    )
    if cover == "uploaded":
        root = tmp_path / "donor"
        root.mkdir()
        raw = PortableCoverLoader(root).load("international_english_news")[0]
        draft = replace(draft, cover_id=CustomCoverAssetStore(root / "custom-covers").save(raw))
    document = donor(tmp_path, draft=draft)
    service = receiver(tmp_path)
    apply(service, document)
    stored = service.store.load_readonly().templates[0]
    bundle = inspect_playlist_bundle(document)
    assert stored.hidden and json.loads(stored.portable_metadata)["configuration"] == {
        "enabled": None,
        "hidden": True,
    }
    assert service.covers.store.load(stored.cover_id) == bundle.jpeg


@pytest.mark.parametrize(
    "failure",
    ["asset_write", "asset_link", "asset_directory_fsync", "bad_existing", "symlink", "fifo"],
)
def test_asset_faults_never_publish_state_and_do_not_overwrite(tmp_path, monkeypatch, failure):
    document = donor(tmp_path)
    service = receiver(tmp_path)
    jpeg = inspect_playlist_bundle(document).jpeg
    cover_id = "uploaded_" + hashlib.sha256(jpeg).hexdigest()
    root = service.covers.store.root
    if failure in {"bad_existing", "symlink", "fifo"}:
        root.mkdir(mode=0o700)
        path = root / (cover_id + ".jpg")
        if failure == "bad_existing":
            path.write_bytes(b"private-existing")
        elif failure == "symlink":
            path.symlink_to(service.store.path)
        else:
            os.mkfifo(path)
    before = service.store.path.read_bytes()

    def fail(*a, **kw):
        raise OSError("private path")

    with monkeypatch.context() as patch:
        if failure == "asset_write":
            patch.setattr(os, "fsync", fail)
        elif failure == "asset_link":
            patch.setattr(os, "link", fail)
        elif failure == "asset_directory_fsync":
            real = os.fsync

            def sync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("private")
                real(fd)

            patch.setattr(os, "fsync", sync)
        with pytest.raises(CustomCoverAssetError):
            apply(service, document)
    assert service.store.path.read_bytes() == before
    assert not service.store.load_readonly().templates
    if failure == "bad_existing":
        assert path.read_bytes() == b"private-existing"
    if failure == "symlink":
        assert path.is_symlink()
    if failure == "fifo":
        assert stat.S_ISFIFO(path.stat().st_mode)
    if failure.startswith("asset_"):
        apply(service, document)
        assert len(service.store.load_readonly().templates) == 1


def test_save_readback_failure_reports_uncertainty_without_second_save(tmp_path, monkeypatch):
    document = donor(tmp_path)
    service = receiver(tmp_path)
    preview = service.preview(document)
    original_load = service.store.load_readonly
    calls = []

    def save(state):
        calls.append(state)

        def unreadable():
            raise OSError("private")

        monkeypatch.setattr(service.store, "load_readonly", unreadable)
        raise OSError("private")

    monkeypatch.setattr(service.store, "save", save)
    with pytest.raises(PlaylistImportError, match="uncertain"):
        service.apply(
            document,
            preview.sources.fingerprint,
            keep_local=preview.sources.conflicts,
            confirm_schema=True,
        )
    assert len(calls) == 1
    monkeypatch.setattr(service.store, "load_readonly", original_load)
    assert not service.store.load_readonly().templates


def test_import_metadata_requires_explicit_v10_and_other_operations_keep_v10(tmp_path):
    from test_source_import import _document

    from news_bulletin_playlist.managed_state import ManagedStateError

    document = donor(tmp_path)
    service = receiver(tmp_path, catalog=BUILTIN_CATALOG)
    apply(service, document)
    state = service.store.load_readonly()
    with pytest.raises(ManagedStateError):
        serialize_managed_state(replace(state, schema_version=9))
    # Adding sources and preparing other legacy identities may not downgrade schema v10.
    preview = service.source_service.preview(_document())
    service.source_service.apply(
        _document(), preview.fingerprint, keep_local=preview.conflicts, confirm_v8=False
    )
    assert service.store.load_readonly().schema_version == 10
    old = service.store.load_readonly()
    admin = ManagedAdminService(service.store, catalog=BUILTIN_CATALOG)
    draft = admin.create_local_template(
        display_name="Manual",
        description="",
        cover_id="custom_default",
        source_ids=("cnn",),
        retention_hours=48,
        max_episodes=100,
        min_duration_minutes=0,
        max_duration_minutes=30,
    )
    assert draft.portable_id and draft.portable_metadata is None
    assert old.templates[0] in service.store.load_readonly().templates


def test_identical_prepared_v9_draft_is_true_noop_without_schema_or_assets(tmp_path, monkeypatch):
    draft = _draft(portable_id=UUID)
    document = donor(tmp_path, draft=draft)
    service = receiver(
        tmp_path, catalog=BUILTIN_CATALOG, state=ManagedState(schema_version=9, templates=(draft,))
    )
    before = files(service.store.path.parent)

    def forbidden(*a, **kw):
        pytest.fail("No-op attempted a write")

    monkeypatch.setattr(service.store, "save", forbidden)
    monkeypatch.setattr(CustomCoverAssetStore, "install_canonical", forbidden)
    preview = service.preview(document)
    result = service.apply(
        document, preview.sources.fingerprint, keep_local=frozenset(), confirm_schema=False
    )
    assert not result.created_draft and service.store.load_readonly().schema_version == 9
    assert files(service.store.path.parent) == before


def test_quarantined_uuid_is_reserved_and_never_overwritten(tmp_path):
    from news_bulletin_playlist.managed_state import QuarantinedPlaylistTemplate

    quarantine = QuarantinedPlaylistTemplate(
        0,
        "invalid_definition",
        json.dumps({"id": "custom_broken", "portable_id": UUID}, sort_keys=True),
    )
    service = receiver(
        tmp_path, state=ManagedState(schema_version=9, quarantined_templates=(quarantine,))
    )
    document = donor(tmp_path)
    before = files(service.store.path.parent)
    assert service.preview(document).status == "uuid_conflict"
    with pytest.raises(PlaylistImportError, match="quarantined"):
        apply(service, document)
    assert files(service.store.path.parent) == before


@pytest.mark.parametrize(
    "damage",
    [
        "encrypted",
        "reserved",
        "zip64",
        "huge_entry",
        "crc",
        "local_time",
        "duplicate_root_key",
        "surrogate",
        "wrong_attribution",
        "ordering",
        "bool_policy",
    ],
)
def test_additional_strict_bounds_and_header_corruption(tmp_path, damage):
    import struct

    document = donor(tmp_path)
    if damage in {"encrypted", "reserved", "zip64", "huge_entry", "crc", "local_time"}:
        raw = bytearray(document)
        with ZipFile(io.BytesIO(document)) as archive:
            offset = archive.start_dir
        if damage == "encrypted":
            struct.pack_into("<H", raw, offset + 8, 1)
        elif damage == "reserved":
            raw[offset + 7] = 3
        elif damage == "zip64":
            struct.pack_into("<H", raw, offset + 6, 45)
        elif damage == "huge_entry":
            struct.pack_into("<I", raw, offset + 24, 2**32 - 1)
        elif damage == "crc":
            struct.pack_into("<I", raw, offset + 16, 0)
        else:
            struct.pack_into("<H", raw, 10, 1)
        document = bytes(raw)
    else:

        def edit(m, d):
            if damage == "surrogate":
                m["playlist"]["display_name"] = "\ud800"
            elif damage == "wrong_attribution":
                m["playlist"]["cover"]["attribution"]["provenance"] = []
            elif damage == "ordering":
                m["playlist"]["ordering"] = "random"
            elif damage == "bool_policy":
                m["playlist"]["max_episodes"] = True

        if damage == "surrogate":
            # Deliberately use escaped UTF-8 JSON for a malformed Unicode scalar.
            def encoded(m, d):
                m["playlist"]["display_name"] = "broken"

            document = rewrite(document, encoded)
            with ZipFile(io.BytesIO(document)) as archive:
                items = [(i, archive.read(i)) for i in archive.infolist()]
            changed = items[0][1].replace(b'"broken"', b'"\\ud800"')
            output = io.BytesIO()
            with ZipFile(output, "w") as archive:
                for i, b in items:
                    if i.filename == "manifest.json":
                        b = changed
                    if i.filename == "manifest.sha256":
                        b = (hashlib.sha256(changed).hexdigest() + "\n").encode()
                    archive.writestr(i, b)
            document = output.getvalue()
        elif damage == "duplicate_root_key":
            with ZipFile(io.BytesIO(document)) as archive:
                items = [(i, archive.read(i)) for i in archive.infolist()]
            changed = items[0][1].replace(b'"version": 1', b'"version": 1, "version": 1')
            output = io.BytesIO()
            with ZipFile(output, "w") as archive:
                for i, b in items:
                    if i.filename == "manifest.json":
                        b = changed
                    if i.filename == "manifest.sha256":
                        b = (hashlib.sha256(changed).hexdigest() + "\n").encode()
                    archive.writestr(i, b)
            document = output.getvalue()
        else:
            document = rewrite(document, edit)
    with pytest.raises(PlaylistBundleError):
        inspect_playlist_bundle(document)


@pytest.mark.parametrize(
    "capability", ["unknown_parser", "future_version", "parameters", "missing_registry"]
)
def test_unsupported_capability_categories_block_entire_zip(tmp_path, monkeypatch, capability):
    def edit(m, d):
        data = json.loads(d["sources.json"])
        parser = data["sources"][0]["processing"]["parser"]
        if capability == "unknown_parser":
            parser["id"] = "unknown"
        elif capability == "future_version":
            parser["version"] = 9
        elif capability == "parameters":
            parser["parameters"] = {"arbitrary": "inert"}
        raw = canonical_json(data)
        d["sources.json"] = raw
        m["files"]["sources.json"] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    document = rewrite(donor(tmp_path), edit)
    if capability == "missing_registry":
        from news_bulletin_playlist.registry import TITLE_PARSERS

        source = inspect_playlist_bundle(document).sources[0].source
        monkeypatch.delitem(TITLE_PARSERS, str(source.parser_id))
    service = receiver(tmp_path)
    before = files(service.store.path.parent)
    preview = service.preview(document)
    assert preview.blocked and preview.sources.unsupported
    with pytest.raises(PortableSourceError):
        apply(service, document)
    assert files(service.store.path.parent) == before


def test_cover_replacement_keeps_origin_observations_but_not_wrong_current_credit(tmp_path):
    document = donor(tmp_path)
    service = receiver(tmp_path)
    apply(service, document)
    state = service.store.load_readonly()
    draft = state.templates[0]
    metadata = draft.portable_metadata
    raw = service.covers.load("international_english_news")[0]
    new_cover = service.covers.store.save(raw)
    service.store.save(replace(state, templates=(replace(draft, cover_id=new_cover),)))
    export = export_service(service.store.path.parent, catalog=EMPTY)
    export.store = service.store
    bundle = inspect_playlist_bundle(export.export("draft", str(draft.id))[1])
    assert (
        bundle.playlist["cover"]["attribution"]["provenance"]
        == "User-uploaded artwork; rights not recorded"
    )
    assert service.store.load_readonly().templates[0].portable_metadata == metadata
    assert service.preview(document).status == "uuid_conflict"


def test_malformed_jpeg_with_valid_manifest_and_file_hashes_is_rejected(tmp_path):
    bad = b"\xff\xd8broken\xff\xd9"
    sha = hashlib.sha256(bad).hexdigest()
    path = f"covers/{sha}.jpg"

    def edit(m, d):
        old = m["playlist"]["cover"]["path"]
        d[old] = bad
        m["playlist"]["cover"].update(path=path, sha256=sha, size=len(bad))
        del m["files"][old]
        m["files"][path] = {"sha256": sha, "size": len(bad)}

    document = rewrite(donor(tmp_path), edit, names={3: path})
    with pytest.raises(PlaylistBundleError):
        inspect_playlist_bundle(document)


def test_compression_rejected_before_any_entry_inflation(tmp_path, monkeypatch):
    document = rewrite(donor(tmp_path), compression=ZIP_DEFLATED)

    def forbidden(*a, **kw):
        pytest.fail("Unsupported compression was inflated")

    monkeypatch.setattr(ZipFile, "open", forbidden)
    with pytest.raises(PlaylistBundleError):
        inspect_playlist_bundle(document)


def test_uuid_comparison_distinguishes_exception_dst_fold(tmp_path):
    from news_bulletin_playlist.portable_playlists import playlist_bundle

    template = BUILTIN_CATALOG.playlists[0]
    exception = DurationPolicyException(
        "dst", template.default_source_ids[0], time(2, 30, fold=0), 3600
    )
    template = replace(template, duration_policy=DurationPolicy(1800, (exception,)))
    catalog = BuiltInCatalog(BUILTIN_CATALOG.sources, (template,))
    p = replace(activate_template(template, "synthetic"), portable_id=UUID)
    initial = ManagedState(schema_version=9, playlists=(p,))
    donor_store = ManagedStateStore(tmp_path / "unused-donor.json", catalog=catalog)
    document = playlist_bundle(
        initial, donor_store, "managed", str(p.id), PortableCoverLoader(tmp_path)
    )[1]

    class MemoryStore(ManagedStateStore):
        state = ManagedState(schema_version=10)

        def load_readonly(self):
            return self.state

    store = MemoryStore(tmp_path / "unused-recipient.json", catalog=catalog)
    service = PlaylistImportService(
        store,
        ConfigurationSynchronization(),
        ProvisioningJournal(tmp_path / "absent-journal.json"),
        PortableCoverLoader(tmp_path),
    )
    candidate = service.preview(document).draft
    store.state = ManagedState(
        schema_version=10, templates=(replace(candidate, cover_id=p.cover_id),)
    )
    assert service.preview(document).status == "identical"
    changed = rewrite(
        document,
        lambda m, d: m["playlist"]["duration_policy"]["exceptions"][0].update(edition_fold=1),
    )
    assert service.preview(changed).status == "uuid_conflict"


def test_ambiguous_readback_does_not_confirm_different_exception_fold(tmp_path, monkeypatch):
    from news_bulletin_playlist.managed_state import same_managed_configuration
    from news_bulletin_playlist.portable_playlists import playlist_bundle

    template = BUILTIN_CATALOG.playlists[0]
    exception = DurationPolicyException(
        "dst", template.default_source_ids[0], time(2, 30, fold=0), 3600
    )
    template = replace(template, duration_policy=DurationPolicy(1800, (exception,)))
    catalog = BuiltInCatalog(BUILTIN_CATALOG.sources, (template,))
    p = replace(activate_template(template, "synthetic"), portable_id=UUID)
    donor_store = ManagedStateStore(tmp_path / "unused-donor.json", catalog=catalog)
    document = playlist_bundle(
        ManagedState(schema_version=9, playlists=(p,)),
        donor_store,
        "managed",
        str(p.id),
        PortableCoverLoader(tmp_path),
    )[1]

    class MemoryStore(ManagedStateStore):
        state = ManagedState(schema_version=10)

        def load_readonly(self):
            return self.state

        def save(self, candidate):
            draft = candidate.templates[0]
            changed = replace(draft.duration_exceptions[0], edition_local_time=time(2, 30, fold=1))
            self.state = replace(
                candidate, templates=(replace(draft, duration_exceptions=(changed,)),)
            )
            assert self.state == candidate  # Python dataclass/time equality hides this difference.
            assert not same_managed_configuration(self.state, candidate, catalog=catalog)
            raise OSError("ambiguous save")

    store = MemoryStore(tmp_path / "unused-recipient.json", catalog=catalog)
    service = PlaylistImportService(
        store,
        ConfigurationSynchronization(),
        ProvisioningJournal(tmp_path / "absent-journal.json"),
        PortableCoverLoader(tmp_path),
    )
    monkeypatch.setattr(
        CustomCoverAssetStore,
        "install_canonical",
        lambda self, data: "uploaded_" + hashlib.sha256(data).hexdigest(),
    )
    with pytest.raises(PlaylistImportError, match="uncertain"):
        apply(service, document)


def test_readback_comparison_preserves_types_inside_quarantine():
    from news_bulletin_playlist.managed_state import (
        QuarantinedPlaylistTemplate,
        same_managed_configuration,
    )

    a = QuarantinedPlaylistTemplate(
        0, "invalid_definition", json.dumps({"invalid": True}, sort_keys=True)
    )
    b = replace(a, raw_json=json.dumps({"invalid": 1}, sort_keys=True))
    left = ManagedState(schema_version=10, quarantined_templates=(a,))
    right = replace(left, quarantined_templates=(b,))
    assert not same_managed_configuration(left, right)


def test_four_public_product_shapes_portable_batch_into_truly_empty_root(tmp_path):
    """Four representative live configurations round-trip without live destinations."""

    selected_ids = (
        "spain_spanish_news",
        "international_english_news",
        "international_polish_news",
        "international_spanish_long_news",
    )
    identities = (
        "b96ab2b3-6568-459c-a391-3a993280e174",
        "abb9730d-d6eb-4235-b9b4-1ea1c127d67c",
        "dd11d0e9-f8d3-4695-8e78-746cda90432d",
        "ea7410ec-f7ef-49ad-a17d-12d7e9da16ca",
    )
    chosen = tuple(
        next(template for template in BUILTIN_CATALOG.playlists if str(template.id) == id_)
        for id_ in selected_ids
    )
    managed = tuple(
        replace(activate_template(template, f"PRIVATE-DESTINATION-{i}"), portable_id=identity)
        for i, (template, identity) in enumerate(zip(chosen, identities, strict=True))
    )
    donor_root = tmp_path / "independent-donor"
    donor_root.mkdir()
    exporter = export_service(donor_root, ManagedState(schema_version=9, playlists=managed))
    # Portable artifacts must not copy incidental credentials, journals or history.
    (donor_root / "oauth.json").write_bytes(b"PRIVATE-OAUTH-SENTINEL")
    (donor_root / "provisioning.json").write_bytes(b"PRIVATE-JOURNAL-SENTINEL")
    (donor_root / "history.sqlite3").write_bytes(b"PRIVATE-HISTORY-SENTINEL")
    donor_before = files(donor_root)

    archives = {
        str(playlist.id): exporter.export("managed", str(playlist.id))[1] for playlist in managed
    }
    assert set(archives) == set(selected_ids)
    assert files(donor_root) == donor_before
    bundles = {id_: inspect_playlist_bundle(data) for id_, data in archives.items()}
    for id_, data in archives.items():
        assert b"PRIVATE-DESTINATION-" not in data
        assert b"PRIVATE-OAUTH-SENTINEL" not in data
        assert b"PRIVATE-JOURNAL-SENTINEL" not in data
        assert b"PRIVATE-HISTORY-SENTINEL" not in data
        assert bundles[id_].playlist["portable_id"] == managed[selected_ids.index(id_)].portable_id

    recipient_root = tmp_path / "truly-empty-recipient"
    recipient_root.mkdir()
    store = ManagedStateStore(recipient_root / "managed-state.json", catalog=EMPTY)
    recipient = PlaylistImportService(
        store,
        ConfigurationSynchronization(),
        ProvisioningJournal(recipient_root / "provisioning.json"),
        PortableCoverLoader(recipient_root),
    )
    assert files(recipient_root) == {}
    assert store.load_readonly().playlists == ()

    for id_ in selected_ids:
        original = files(recipient_root)
        preview = recipient.preview(archives[id_])
        assert preview.status == "create" and not preview.blocked
        assert files(recipient_root) == original
        assert apply(recipient, archives[id_]).created_draft

    imported_state = store.load_readonly()
    assert imported_state.schema_version == 10
    assert not imported_state.playlists  # No Spotify destination may be activated.
    assert len(imported_state.templates) == 4
    drafts = {template.portable_id: template for template in imported_state.templates}
    assert set(drafts) == set(identities)

    installed = {
        source.external_references[0].external_id: source
        for source in effective_sources(EMPTY, imported_state)
    }
    expected_shows = {
        definition.external_references[0].external_id
        for bundle in bundles.values()
        for definition in (record.source for record in bundle.sources)
    }
    assert set(installed) == expected_shows
    exported = export_service(recipient_root, catalog=EMPTY)
    for id_, identity in zip(selected_ids, identities, strict=True):
        donor_bundle = bundles[id_]
        draft = drafts[identity]
        original = managed[selected_ids.index(id_)]
        assert draft.display_name == original.display_name
        assert draft.description == original.description
        assert draft.retention_hours == original.retention_hours
        assert draft.max_episodes == original.max_episodes
        assert draft.min_duration_seconds == original.min_duration_seconds
        assert draft.max_duration_seconds == original.max_duration_seconds
        assert draft.countries == original.countries
        assert draft.languages == original.languages
        assert draft.hidden is False
        assert (
            CustomCoverAssetStore(recipient_root / "custom-covers").load(draft.cover_id)
            == donor_bundle.jpeg
        )
        for record in donor_bundle.sources:
            definition = record.source
            show_id = definition.external_references[0].external_id
            assert replace(installed[show_id], id=definition.id) == definition
        roundtrip_bundle = inspect_playlist_bundle(exported.export("draft", str(draft.id))[1])
        assert roundtrip_bundle.jpeg == donor_bundle.jpeg
        assert roundtrip_bundle.playlist["portable_id"] == identity
        assert roundtrip_bundle.playlist["retention_hours"] == original.retention_hours
        assert roundtrip_bundle.playlist["max_episodes"] == original.max_episodes
        assert (
            roundtrip_bundle.playlist["duration_policy"] == donor_bundle.playlist["duration_policy"]
        )

    # Simulate a process restart; repeated preview and import remain read-only
    # or identical no-ops, with no additional assets or duplicate sources.
    restarted = PlaylistImportService(
        ManagedStateStore(store.path, catalog=EMPTY),
        ConfigurationSynchronization(),
        ProvisioningJournal(recipient_root / "provisioning.json"),
        PortableCoverLoader(recipient_root),
    )
    snapshot = files(recipient_root)
    for id_ in selected_ids:
        preview = restarted.preview(archives[id_])
        assert preview.status == "identical"
        assert not apply(restarted, archives[id_]).created_draft
    assert files(recipient_root) == snapshot
    assert files(donor_root) == donor_before
