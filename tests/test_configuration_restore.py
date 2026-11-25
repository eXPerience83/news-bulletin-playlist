"""Restore a real portable export into isolated fresh v11 roots, without provider I/O."""

import json
import socket
from dataclasses import replace
from uuid import uuid4

import pytest
from _file_snapshots import snapshot_files as _files
from _legacy_dataset import legacy_root
from _playlist_template_fixtures import _draft

from news_bulletin_playlist import configuration_restore
from news_bulletin_playlist.configuration_export import (
    export_configuration,
    inspect_configuration,
)
from news_bulletin_playlist.configuration_restore import (
    ConfigurationRestoreError,
    ConfigurationRestoreService,
)
from news_bulletin_playlist.custom_cover_assets import CustomCoverAssetStore
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_provisioning import (
    MANAGED_PROVISIONING_FILENAME,
    ProvisioningIntent,
    ProvisioningJournal,
    ProvisioningState,
)
from news_bulletin_playlist.managed_state import (
    PLAYLIST_ORDERING_SCHEMA_VERSION,
    ManagedStateError,
    activate_template,
)
from news_bulletin_playlist.models import OrderingPolicy
from news_bulletin_playlist.owner_inventory_export import inspect_owner_inventory
from news_bulletin_playlist.playlist_bundle import inspect_playlist_bundle_v3
from news_bulletin_playlist.playlist_collection import inspect_collection
from news_bulletin_playlist.v11_managed_adapter import ForwardOnlyManagedStateStore


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Configuration restore attempted network I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


def _donor(tmp_path, *, local_managed=False):
    root, _ = legacy_root(tmp_path, 10)
    store = ForwardOnlyManagedStateStore(root)
    state = store.load_readonly()
    draft = _draft(portable_id=str(uuid4()), hidden=True)
    playlists = tuple(
        replace(
            p,
            portable_id=str(uuid4()),
            ordering_override=OrderingPolicy.LATEST_PER_SOURCE_FIRST,
        )
        for p in state.playlists
    )
    if local_managed:
        playlists += (
            replace(
                activate_template(draft.as_playlist_template(), "private-local"),
                portable_id=str(uuid4()),
                enabled=False,
            ),
        )
    store.save(
        replace(
            state,
            schema_version=PLAYLIST_ORDERING_SCHEMA_VERSION,
            playlists=playlists,
            templates=(draft,),
        )
    )
    return export_configuration(root, ConfigurationSynchronization())


def _receiver(tmp_path):
    root = tmp_path / "receiver"
    root.mkdir()
    store = ForwardOnlyManagedStateStore(root)
    journal = ProvisioningJournal(root / MANAGED_PROVISIONING_FILENAME)
    assets = CustomCoverAssetStore(root / "custom-covers")
    sync = ConfigurationSynchronization()
    return root, ConfigurationRestoreService(store, sync, journal, assets)


def test_restore_preview_confirm_restart_and_reexport(tmp_path):
    document = _donor(tmp_path)
    root, service = _receiver(tmp_path)
    before = _files(root)
    preview = service.preview(document)
    assert preview.recovered_managed == 4
    assert preview.original_drafts == 1
    assert preview.owner_templates > preview.recovered_managed
    assert before == _files(root)
    with pytest.raises(ConfigurationRestoreError, match="confirmation"):
        service.apply(document, preview.fingerprint)
    with pytest.raises(ConfigurationRestoreError, match="stale"):
        service.apply(document, "0" * 64, confirmed=True)
    assert before == _files(root)

    result = service.apply(document, preview.fingerprint, confirmed=True)
    assert not result.observed_after_error
    restarted = ForwardOnlyManagedStateStore(root, readonly=True)
    state = restarted.load_readonly()
    assert not state.playlists
    assert len(state.templates) == 5
    original = inspect_configuration(document)
    restored = inspect_configuration(export_configuration(root, service.synchronization))
    initial_owner = inspect_owner_inventory(original.owner_document)
    final_owner = inspect_owner_inventory(restored.owner_document)
    assert initial_owner.inventory.sources == final_owner.inventory.sources
    assert initial_owner.inventory.source_order == final_owner.inventory.source_order
    assert initial_owner.inventory.retired_source_ids == final_owner.inventory.retired_source_ids
    assert len(initial_owner.artwork) == len(final_owner.artwork)
    assert {a.jpeg for a in initial_owner.artwork} == {a.jpeg for a in final_owner.artwork}
    for old, new in zip(
        initial_owner.inventory.templates.records,
        final_owner.inventory.templates.records,
        strict=True,
    ):
        assert replace(new, cover_id=old.cover_id) == old
    restored_members = {m.portable_id: m for m in inspect_collection(restored.collection_document)}
    original_members = inspect_collection(original.collection_document)
    assert set(restored_members) == {m.portable_id for m in original_members}
    for member in original_members:
        old = inspect_playlist_bundle_v3(member.document)
        new = inspect_playlist_bundle_v3(restored_members[member.portable_id].document)
        assert new.playlist["kind"] == "draft"
        for field in (
            "display_name",
            "description",
            "source_ids",
            "countries",
            "languages",
            "retention_hours",
            "max_episodes",
            "duration_policy",
            "ordering_override",
        ):
            assert new.playlist[field] == old.playlist[field]
        assert new.jpeg == old.jpeg
        recovered = next(d for d in state.templates if d.portable_id == member.portable_id)
        assert (
            json.loads(recovered.portable_metadata)["configuration"]
            == (old.playlist["configuration"])
        )
        assert recovered.hidden == (
            old.playlist["configuration"]["hidden"] if member.kind == "draft" else False
        )
    before_retry = _files(root)
    identical = service.preview(document)
    assert identical.already_restored
    assert service.apply(document, identical.fingerprint, confirmed=True).preview.already_restored
    assert before_retry == _files(root)
    with pytest.raises(ConfigurationRestoreError, match="stale"):
        service.apply(document, preview.fingerprint, confirmed=True)
    other = tmp_path / "other"
    other.mkdir()
    different = _donor(other)
    with pytest.raises(ConfigurationRestoreError, match="overwrite"):
        service.preview(different)
    assert before_retry == _files(root)


def test_local_draft_association_fails_closed(tmp_path):
    document = _donor(tmp_path, local_managed=True)
    root, service = _receiver(tmp_path)
    before = _files(root)
    with pytest.raises(ConfigurationRestoreError, match="Managed-to-local-draft"):
        service.preview(document)
    assert before == _files(root)


def test_pending_intent_blocks_confirmed_apply(tmp_path):
    document = _donor(tmp_path)
    root, service = _receiver(tmp_path)
    preview = service.preview(document)
    service.provisioning.save(
        ProvisioningIntent(
            state=ProvisioningState.REQUEST_STARTED,
            template_id="spain_spanish_news",
            display_name="Pending",
            description="",
            cover_id="spain_spanish_news",
            source_ids=("cnn",),
            retention_hours=48,
            max_episodes=100,
            max_duration_seconds=1800,
        )
    )
    before = _files(root)
    with pytest.raises(ConfigurationRestoreError, match="without intent"):
        service.apply(document, preview.fingerprint, confirmed=True)
    assert before == _files(root)


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_cas_readback_no_blind_retry(tmp_path, monkeypatch, committed):
    document = _donor(tmp_path)
    _, service = _receiver(tmp_path)
    preview = service.preview(document)
    original_save = service.store.save_owner_bundle
    calls = 0

    def fail(inventory, overlay):
        nonlocal calls
        calls += 1
        if committed:
            original_save(inventory, overlay)
        raise ManagedStateError("fake failure")

    monkeypatch.setattr(service.store, "save_owner_bundle", fail)
    if committed:
        result = service.apply(document, preview.fingerprint, confirmed=True)
        assert result.observed_after_error
        assert len(service.store.load_readonly().templates) == 5
    else:
        with pytest.raises(ConfigurationRestoreError, match="not committed"):
            service.apply(document, preview.fingerprint, confirmed=True)
        assert not service.store.load_readonly().templates
        assert not service.store.catalog.sources
    assert calls == 1


def test_asset_failure_never_publishes_configuration(tmp_path, monkeypatch):
    document = _donor(tmp_path)
    _, service = _receiver(tmp_path)
    preview = service.preview(document)
    previous = service.store._expected_bytes
    original = service.assets.install_canonical
    calls = 0

    def fail_second(jpeg):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fake failure")
        return original(jpeg)

    monkeypatch.setattr(service.assets, "install_canonical", fail_second)
    with pytest.raises(ConfigurationRestoreError):
        service.apply(document, preview.fingerprint, confirmed=True)
    assert calls == 2
    state = service.store.load_readonly()
    assert not state.templates
    assert not state.playlists
    assert not service.store.catalog.sources
    assert service.store._expected_bytes == previous


def test_restore_size_limit_before_inspection(tmp_path, monkeypatch):
    document = _donor(tmp_path)
    root, service = _receiver(tmp_path)
    before = _files(root)
    monkeypatch.setattr(configuration_restore, "MAX_RESTORE_BYTES", len(document) - 1)

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized restore reached inspector")

    monkeypatch.setattr(configuration_restore, "inspect_configuration", forbidden)
    with pytest.raises(ConfigurationRestoreError, match="size limit"):
        service.preview(document)
    assert before == _files(root)
