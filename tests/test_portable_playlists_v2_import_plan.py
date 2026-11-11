"""Lossless, read-only ZIP v2 import planning for an empty v11 owner installation."""

from __future__ import annotations

from dataclasses import replace
from datetime import time

from _portable_playlist_fixtures import UUID, _service

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal
from news_bulletin_playlist.managed_state import ManagedState, activate_template
from news_bulletin_playlist.models import DurationPolicy, DurationPolicyException
from news_bulletin_playlist.playlist_import import PlaylistImportService
from news_bulletin_playlist.portable_playlists import PortableCoverLoader
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
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
