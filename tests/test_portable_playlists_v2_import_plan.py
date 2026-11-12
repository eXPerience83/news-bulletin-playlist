"""Lossless, read-only ZIP v2 import planning for an empty v11 owner installation."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import time

import pytest
from _portable_playlist_fixtures import UUID, _service, rewrite

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.custom_cover_assets import CustomCoverAssetStore
from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedState,
    ManagedStateError,
    QuarantinedPlaylistTemplate,
    activate_template,
)
from news_bulletin_playlist.models import DurationPolicy, DurationPolicyException, PlaylistId
from news_bulletin_playlist.playlist_bundle import inspect_playlist_bundle_v2
from news_bulletin_playlist.playlist_import import PlaylistImportError, PlaylistImportService
from news_bulletin_playlist.portable_playlists import (
    PlaylistExportError,
    PlaylistExportService,
    PortableCoverLoader,
)
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_codec import parse_v11_operational
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME, V11StoreError


def _donor(tmp_path, *, different_cover=False):
    template = BUILTIN_CATALOG.playlists[0]
    first, last = template.default_source_ids[0], template.default_source_ids[-1]
    first_exception = DurationPolicyException("active", first, time(2, 15, 0, 123456, fold=1), 3600)
    dormant = DurationPolicyException("dormant", last, time(13, 30, fold=1), 2700)
    template = replace(
        template,
        duration_policy=DurationPolicy(
            default_max_seconds=1800, exceptions=(first_exception, dormant)
        ),
    )
    donor_root = tmp_path / "donor"
    donor_root.mkdir()
    catalog = BuiltInCatalog(BUILTIN_CATALOG.sources, (template,))
    playlist = replace(
        activate_template(template, "SECRET-SPOTIFY-LINK"),
        portable_id=UUID,
        source_ids=(first,),
        max_duration_seconds=3000,
        cover_id=(BUILTIN_CATALOG.playlists[1].cover_id if different_cover else template.cover_id),
    )
    service = _service(
        donor_root,
        ManagedState(schema_version=9, playlists=(playlist,)),
        catalog=catalog,
    )
    return service.export("managed", str(playlist.id), version=2)[1], template


def _receiver(tmp_path):
    root = tmp_path / "receiver"
    root.mkdir()
    store = open_managed_state_store(root)
    service = PlaylistImportService(
        store,
        ConfigurationSynchronization(),
        ProvisioningJournal(root / "provisioning.json"),
        PortableCoverLoader(root),
    )
    return root, store, service


def test_v2_plan_restores_dormant_source_and_fold_without_writes(tmp_path):
    data, template = _donor(tmp_path)
    root, store, importer = _receiver(tmp_path)
    assert store.catalog.sources == () and store.catalog.playlists == ()
    baseline = (root / V11_OPERATIONAL_FILENAME).read_bytes()
    first = importer.preview_v2(data)
    assert not first.blocked
    assert first.owner_template is not None
    assert first.selected_source_ids is not None
    assert first.sources.creates == len(template.default_source_ids)
    assert len(first.selected_source_ids) == 1
    assert first.owner_template.id.startswith("custom_")
    assert set(first.owner_template.default_source_ids) == {
        row.local_id for row in first.sources.rows
    }
    assert len(first.owner_template.duration_policy.exceptions) == 2
    assert first.owner_template.duration_policy.exceptions[1].edition_local_time.fold == 1
    assert (
        first.owner_template.duration_policy.exceptions[0].edition_local_time.microsecond == 123456
    )
    assert all(
        e.source_id in first.owner_template.default_source_ids
        for e in first.owner_template.duration_policy.exceptions
    )
    assert first == importer.preview_v2(data)
    assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == baseline
    assert store.load_readonly().playlists == ()


def test_v2_plan_blocks_source_metadata_collision_without_write(tmp_path):
    data, template = _donor(tmp_path)
    root, store, importer = _receiver(tmp_path)
    from news_bulletin_playlist.managed_state import InstalledSource

    first = next(
        source for source in BUILTIN_CATALOG.sources if source.id == template.default_source_ids[0]
    )
    state = store.load_readonly()
    store.save(
        replace(
            state,
            sources=(replace(InstalledSource.from_definition(first), enabled=False),),
        )
    )
    before = (root / V11_OPERATIONAL_FILENAME).read_bytes()
    plan = importer.preview_v2(data)
    assert plan.blocked and plan.sources.conflicts
    assert plan.owner_template is None
    assert plan.selected_source_ids is None
    assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == before


def test_v2_import_commits_full_owner_and_current_projection_atomically(tmp_path):
    data, template = _donor(tmp_path)
    root, store, importer = _receiver(tmp_path)
    before = (root / V11_OPERATIONAL_FILENAME).read_bytes()
    plan = importer.preview_v2(data)
    result = importer.apply_v2(data, plan.sources.fingerprint)
    assert result.created_sources == len(template.default_source_ids)
    assert result.owner_template_id != result.current_draft_id
    assert not result.recovered
    assert (root / V11_OPERATIONAL_FILENAME).read_bytes() != before
    assert not (root / "managed-state.json").exists()
    reopened = open_managed_state_store(root)
    state = reopened.load_readonly()
    assert state.playlists == ()
    assert state.sources == ()
    assert len(state.templates) == 1
    assert state.templates[0].id == result.current_draft_id
    assert state.templates[0].portable_id == UUID
    assert state.templates[0].owner_template_id == result.owner_template_id
    assert state.templates[0].max_duration_seconds == 3000
    assert len(state.templates[0].default_source_ids) == 1
    assert len(state.templates[0].duration_exceptions) == 1
    assert len(reopened.catalog.sources) == len(template.default_source_ids)
    assert len(reopened.catalog.playlists) == 1
    owner_template = reopened.catalog.playlists[0]
    assert owner_template.id == result.owner_template_id
    assert len(owner_template.default_source_ids) == len(template.default_source_ids)
    assert len(owner_template.duration_policy.exceptions) == 2
    assert owner_template.duration_policy.exceptions[1].edition_local_time.fold == 1
    assert owner_template.duration_policy.exceptions[0].edition_local_time.microsecond == 123456
    assert owner_template.default_source_ids[-1] not in state.templates[0].default_source_ids
    original_by_show = {s.external_references[0].external_id: s for s in BUILTIN_CATALOG.sources}
    for restored in reopened.catalog.sources:
        show = restored.external_references[0].external_id
        expected = original_by_show[show]
        assert restored.parser_id == expected.parser_id
        assert restored.collection_filter_id == expected.collection_filter_id
        assert restored.temporal_strategy == expected.temporal_strategy
        assert restored.timezone == expected.timezone
    v11 = parse_v11_operational((root / V11_OPERATIONAL_FILENAME).read_bytes())
    assert len(v11.owner_inventory.cover_attributions) == 1
    covers = CustomCoverAssetStore(root / "custom-covers")
    assert covers.load(owner_template.cover_id)
    assert covers.load(state.templates[0].cover_id)
    assert importer.preview_v2(data).blocked


def test_v2_apply_refuses_stale_preview_without_installing_artwork(tmp_path):
    data, _ = _donor(tmp_path)
    root, store, importer = _receiver(tmp_path)
    plan = importer.preview_v2(data)
    state = store.load_readonly()
    # Fresh v11 already stores schema 10; make a real unrelated durable edit.
    store.save(
        replace(
            state,
            sources=(InstalledSource.from_definition(BUILTIN_CATALOG.sources[0]),),
        )
    )
    before = (root / V11_OPERATIONAL_FILENAME).read_bytes()
    with pytest.raises(PlaylistImportError, match="changed"):
        importer.apply_v2(data, plan.sources.fingerprint)
    assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == before
    assert not (root / "custom-covers").exists()


def test_v2_failed_cas_cannot_publish_partial_sources_or_templates(tmp_path, monkeypatch):
    data, _ = _donor(tmp_path)
    root, store, importer = _receiver(tmp_path)
    plan = importer.preview_v2(data)
    before = (root / V11_OPERATIONAL_FILENAME).read_bytes()

    def fail_before_commit(*_args, **_kwargs):
        raise ManagedStateError("synthetic interrupted CAS")

    monkeypatch.setattr(store, "save_owner_bundle", fail_before_commit)
    with pytest.raises(PlaylistImportError, match="not committed"):
        importer.apply_v2(data, plan.sources.fingerprint)
    assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == before
    reopened = open_managed_state_store(root)
    assert not reopened.catalog.sources
    assert not reopened.catalog.playlists
    assert not reopened.load_readonly().templates
    assert not (root / "managed-state.json").exists()


def test_v2_reexport_restored_draft_preserves_owner_closure_on_second_install(tmp_path):
    original, historical = _donor(tmp_path, different_cover=True)
    root, store, importer = _receiver(tmp_path)
    first = importer.preview_v2(original)
    first_result = importer.apply_v2(original, first.sources.fingerprint)
    exporter = PlaylistExportService(
        store, ConfigurationSynchronization(), PortableCoverLoader(root)
    )
    with pytest.raises(PlaylistExportError, match="ZIP v2"):
        exporter.export("draft", first_result.current_draft_id)
    identity, outgoing = exporter.export("draft", first_result.current_draft_id, version=2)
    assert identity == UUID
    parsed = inspect_playlist_bundle_v2(outgoing)
    assert parsed.playlist["kind"] == "draft"
    assert parsed.template is not None
    assert len(parsed.template.default_source_ids) == len(historical.default_source_ids)
    assert len(parsed.template.duration_policy.exceptions) == 2
    assert len(parsed.playlist["source_ids"]) == 1
    before = inspect_playlist_bundle_v2(original)
    assert before.jpeg != before.template_jpeg
    assert parsed.jpeg == before.jpeg
    assert parsed.template_jpeg == before.template_jpeg
    assert parsed.template_attribution == before.template_attribution
    assert b"SECRET-SPOTIFY-LINK" not in outgoing

    separate = tmp_path / "second"
    separate.mkdir()
    _root_2, store_2, importer_2 = _receiver(separate)
    second = importer_2.preview_v2(outgoing)
    assert not second.blocked
    second_result = importer_2.apply_v2(outgoing, second.sources.fingerprint)
    assert second_result.created_sources == len(historical.default_source_ids)
    assert len(store_2.catalog.playlists[0].duration_policy.exceptions) == 2
    assert len(store_2.load_readonly().templates[0].default_source_ids) == 1
    assert store_2.load_readonly().templates[0].owner_template_id == second_result.owner_template_id


def test_v2_owner_template_cannot_be_deleted_while_current_draft_references_it(tmp_path):
    data, _historical = _donor(tmp_path)
    root, store, importer = _receiver(tmp_path)
    plan = importer.preview_v2(data)
    result = importer.apply_v2(data, plan.sources.fingerprint)
    before = (root / V11_OPERATIONAL_FILENAME).read_bytes()
    with pytest.raises(ManagedStateError, match="could not be deleted"):
        store.delete_owner_template(PlaylistId(result.owner_template_id))
    assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == before
    assert store.load_readonly().templates[0].owner_template_id == result.owner_template_id


def test_v2_reassignment_duration_edits_and_source_toggle_preserve_original_owner(tmp_path):
    data, historical = _donor(tmp_path)
    root, store, importer = _receiver(tmp_path)
    plan = importer.preview_v2(data)
    result = importer.apply_v2(data, plan.sources.fingerprint)
    reopened = open_managed_state_store(root)
    original_owner = reopened.catalog.playlists[0]
    assert len(original_owner.duration_policy.exceptions) == 2
    current = reopened.load_readonly()
    restored = current.templates[0]
    edited = replace(
        restored,
        default_source_ids=original_owner.default_source_ids,
        max_duration_seconds=2000,
        duration_exceptions=original_owner.duration_policy.exceptions,
    )
    reopened.save(replace(current, templates=(edited,)))
    assert reopened.load_readonly().templates[0].owner_template_id == result.owner_template_id
    owned_source = reopened.catalog.sources[0]
    from news_bulletin_playlist.managed_state import InstalledSource

    reopened.save_owner_source(
        replace(InstalledSource.from_definition(owned_source), enabled=False)
    )
    disabled = open_managed_state_store(root)
    assert not disabled.catalog.sources[0].enabled
    assert disabled.catalog.sources[0].parser_id == owned_source.parser_id
    assert disabled.catalog.playlists[0].duration_policy.exceptions == (
        original_owner.duration_policy.exceptions
    )
    disabled.save_owner_source(
        replace(InstalledSource.from_definition(disabled.catalog.sources[0]), enabled=True)
    )
    assert open_managed_state_store(root).catalog.sources[0].enabled

    exporter = PlaylistExportService(
        disabled, ConfigurationSynchronization(), PortableCoverLoader(root)
    )
    outgoing = exporter.export("draft", result.current_draft_id, version=2)[1]
    parsed = inspect_playlist_bundle_v2(outgoing)
    assert parsed.template is not None
    assert parsed.template.duration_policy == original_owner.duration_policy
    assert parsed.playlist["duration_policy"]["max_seconds"] == 2000
    assert len(parsed.playlist["source_ids"]) == len(historical.default_source_ids)
    assert len(parsed.exceptions) == 2
    assert parsed.template.id == original_owner.id
    assert (root / "managed-state.json").exists() is False


def test_v2_quarantined_portable_uuid_cannot_be_reinstalled(tmp_path):
    data, _ = _donor(tmp_path)
    root, store, importer = _receiver(tmp_path)
    state = store.load_readonly()
    quarantined = QuarantinedPlaylistTemplate(
        source_index=0,
        reason="invalid_definition",
        raw_json=json.dumps({"id": "custom_bad", "portable_id": UUID}, sort_keys=True),
    )
    store.save(replace(state, quarantined_templates=(quarantined,)))
    before = (root / V11_OPERATIONAL_FILENAME).read_bytes()
    plan = importer.preview_v2(data)
    assert plan.blocked
    assert "identity" in plan.reason.lower()
    assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == before


def test_v2_restore_sorts_current_draft_with_existing_unrelated_draft(tmp_path):
    data, historical = _donor(tmp_path)
    _root, store, importer = _receiver(tmp_path)
    unrelated_source = next(
        s for s in BUILTIN_CATALOG.sources if s.id not in historical.default_source_ids
    )
    existing = LocalPlaylistTemplate(
        id=PlaylistId("custom_zzzz_existing"),
        display_name="Existing local draft",
        description="",
        default_source_ids=(unrelated_source.id,),
        cover_id="custom_default",
    )
    state = store.load_readonly()
    store.save(
        replace(
            state,
            sources=(InstalledSource.from_definition(unrelated_source),),
            templates=(existing,),
        )
    )
    plan = importer.preview_v2(data)
    assert not plan.blocked
    result = importer.apply_v2(data, plan.sources.fingerprint)
    assert tuple(t.id for t in store.load_readonly().templates) == tuple(
        sorted((existing.id, result.current_draft_id))
    )
    assert len(store.catalog.sources) == len(historical.default_source_ids)


def test_v2_recovered_post_commit_durability_exception_refreshes_adapter(tmp_path, monkeypatch):
    data, _ = _donor(tmp_path)
    root, store, importer = _receiver(tmp_path)
    preview = importer.preview_v2(data)
    real_save = store._owner.save_owner_inventory

    def publish_then_raise(*args, **kwargs):
        real_save(*args, **kwargs)
        # On disk, the owner inventory has changed; the facade remains stale.
        assert store.catalog.sources == ()
        raise V11StoreError("synthetic error after fsync")

    monkeypatch.setattr(store._owner, "save_owner_inventory", publish_then_raise)
    result = importer.apply_v2(data, preview.sources.fingerprint)
    assert result.recovered
    assert store.catalog.playlists
    assert store.catalog.sources
    assert store.load_readonly().templates[0].owner_template_id == result.owner_template_id
    assert (root / V11_OPERATIONAL_FILENAME).is_file()


def test_v2_activated_managed_playlist_keeps_owner_and_current_cover_credit(tmp_path):
    original, historical = _donor(tmp_path, different_cover=True)
    root, store, importer = _receiver(tmp_path)
    preview = importer.preview_v2(original)
    importer.apply_v2(original, preview.sources.fingerprint)
    state = store.load_readonly()
    linked = state.templates[0]
    activated = replace(
        activate_template(linked.as_playlist_template(), "NOT-A-REAL-DESTINATION"),
        portable_id="123e4567-e89b-42d3-a456-426614174001",
    )
    store.save(replace(state, playlists=(activated,)))
    exporter = PlaylistExportService(
        store, ConfigurationSynchronization(), PortableCoverLoader(root)
    )
    with pytest.raises(PlaylistExportError, match="ZIP v1"):
        exporter.export("managed", str(activated.id))
    output = exporter.export("managed", str(activated.id), version=2)[1]
    restored = inspect_playlist_bundle_v2(output)
    first = inspect_playlist_bundle_v2(original)
    assert restored.template is not None
    assert restored.template.duration_policy.default_max_seconds == (
        first.template.duration_policy.default_max_seconds
    )
    assert tuple(
        (e.id, e.edition_local_time.isoformat(), e.edition_local_time.fold, e.max_seconds)
        for e in restored.template.duration_policy.exceptions
    ) == tuple(
        (e.id, e.edition_local_time.isoformat(), e.edition_local_time.fold, e.max_seconds)
        for e in first.template.duration_policy.exceptions
    )
    assert len(restored.template.default_source_ids) == len(historical.default_source_ids)
    assert restored.jpeg == first.jpeg
    assert restored.template_jpeg == first.template_jpeg
    assert restored.playlist["cover"]["attribution"] == first.playlist["cover"]["attribution"]
    assert restored.template_attribution == first.template_attribution
    assert b"NOT-A-REAL-DESTINATION" not in output


def test_v2_multiple_playlists_reuse_identical_owner_sources_without_duplication(tmp_path):
    first_zip, original_template = _donor(tmp_path)

    def second_playlist_identity(manifest, _entries):
        manifest["playlist"]["portable_id"] = "123e4567-e89b-42d3-a456-426614174002"
        manifest["playlist"]["display_name"] = "Another managed playlist using same stations"

    second_zip = rewrite(first_zip, second_playlist_identity)
    _root, store, importer = _receiver(tmp_path)
    first_plan = importer.preview_v2(first_zip)
    assert not first_plan.blocked
    first_result = importer.apply_v2(first_zip, first_plan.sources.fingerprint)
    assert first_result.created_sources == len(original_template.default_source_ids)
    first_ids = tuple(source.id for source in store.catalog.sources)
    next_plan = importer.preview_v2(second_zip)
    assert not next_plan.blocked
    assert next_plan.sources.creates == 0
    assert all(row.status in {"identical", "reuse"} for row in next_plan.sources.rows)
    second_result = importer.apply_v2(second_zip, next_plan.sources.fingerprint)
    assert second_result.created_sources == 0
    assert tuple(source.id for source in store.catalog.sources) == first_ids
    assert len(store.catalog.playlists) == 2
    assert len(store.load_readonly().templates) == 2
    assert store.load_readonly().playlists == ()
    assert {t.owner_template_id for t in store.load_readonly().templates} == {
        first_result.owner_template_id,
        second_result.owner_template_id,
    }
    assert not _root.joinpath("managed-state.json").exists()
