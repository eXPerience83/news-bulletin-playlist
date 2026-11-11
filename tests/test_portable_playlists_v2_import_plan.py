"""Lossless, read-only ZIP v2 import planning for an empty v11 owner installation."""

from __future__ import annotations

from dataclasses import replace
from datetime import time

import pytest
from _portable_playlist_fixtures import UUID, _service

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.custom_cover_assets import CustomCoverAssetStore
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal
from news_bulletin_playlist.managed_state import ManagedState, ManagedStateError, activate_template
from news_bulletin_playlist.models import DurationPolicy, DurationPolicyException
from news_bulletin_playlist.playlist_import import PlaylistImportError, PlaylistImportService
from news_bulletin_playlist.portable_playlists import PortableCoverLoader
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_codec import parse_v11_operational
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME


def _donor(tmp_path):
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
    store.save(replace(state, schema_version=10))
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
