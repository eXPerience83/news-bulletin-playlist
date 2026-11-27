"""Restore a real portable export into isolated fresh v11 roots, without provider I/O."""

import json
import socket
from dataclasses import replace
from datetime import time
from uuid import uuid4

import pytest
from _file_snapshots import snapshot_files as _files
from _legacy_dataset import legacy_root
from _playlist_template_fixtures import _draft

from news_bulletin_playlist import configuration_export, configuration_restore
from news_bulletin_playlist.configuration_export import (
    ConfigurationExportError,
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
from news_bulletin_playlist.models import DurationPolicyException, OrderingPolicy
from news_bulletin_playlist.owner_inventory_export import inspect_owner_inventory
from news_bulletin_playlist.playlist_bundle import inspect_playlist_bundle_v3
from news_bulletin_playlist.playlist_collection import inspect_collection
from news_bulletin_playlist.portable_playlists import PortableCoverLoader
from news_bulletin_playlist.v11_managed_adapter import ForwardOnlyManagedStateStore


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Configuration restore attempted network I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


def _donor(tmp_path, *, local_managed=False, linked=False):
    root, _ = legacy_root(tmp_path, 10)
    store = ForwardOnlyManagedStateStore(root)
    state = store.load_readonly()
    draft = _draft(
        portable_id=str(uuid4()),
        hidden=True,
        owner_template_id=(
            store.catalog.playlist("international_english_news").id if linked else None
        ),
    )
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
                source_ids=(draft.default_source_ids[0],),
                retention_hours=36,
                ordering_override=OrderingPolicy.LATEST_PER_SOURCE_FIRST,
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


@pytest.mark.parametrize("linked", [False, True])
def test_local_draft_association_survives_restore_and_reexport(tmp_path, linked):
    document = _donor(tmp_path, local_managed=True, linked=linked)
    original = inspect_configuration(document)
    link = next(link for link in original.managed_links if link.template_kind == "draft")
    root, service = _receiver(tmp_path)
    before = _files(root)
    preview = service.preview(document)
    assert preview.original_drafts == 1
    assert preview.recovered_managed == 5
    assert before == _files(root)
    service.apply(document, preview.fingerprint, confirmed=True)

    def check_state(store):
        state = store.load_readonly()
        assert not state.playlists
        assert len(state.templates) == 6
        original_draft = next(d for d in state.templates if d.portable_id == link.template_id)
        recovered = next(d for d in state.templates if d.portable_id == link.playlist_uuid)
        assert original_draft.hidden
        assert not recovered.hidden
        assert original_draft.owner_template_id == recovered.owner_template_id
        assert (recovered.owner_template_id is not None) == linked
        assert recovered.retention_hours == 36
        assert original_draft.retention_hours == 72
        assert recovered.default_source_ids == original_draft.default_source_ids[:1]
        assert recovered.ordering_override is OrderingPolicy.LATEST_PER_SOURCE_FIRST
        metadata = json.loads(recovered.portable_metadata)
        assert metadata["kind"] == "managed"
        assert metadata["configuration"] == {"enabled": False, "hidden": None}
        assert metadata["recovered_from_draft_uuid"] == link.template_id
        return state

    first_state = check_state(ForwardOnlyManagedStateStore(root, readonly=True))
    reexport = export_configuration(root, service.synchronization)
    package = inspect_configuration(reexport)
    assert dict(package.draft_provenance)[link.playlist_uuid] == next(
        d.portable_metadata for d in first_state.templates if d.portable_id == link.playlist_uuid
    )
    assert len(package.draft_provenance) == 6
    assert not package.managed_links
    second = tmp_path / "second"
    second.mkdir()
    _, receiver = _receiver(second)
    preview = receiver.preview(reexport)
    receiver.apply(reexport, preview.fingerprint, confirmed=True)
    assert check_state(receiver.store) == first_state
    assert receiver.preview(reexport).already_restored
    assert service.preview(document).already_restored

    # The optional manifest extension is closed over existing draft UUIDs.
    unknown_provenance = ((str(uuid4()), package.draft_provenance[0][1]),)
    unknown = replace(package, draft_provenance=unknown_provenance)
    with pytest.raises(ConfigurationExportError, match="missing draft"):
        inspect_configuration(configuration_export._encode(unknown))
    metadata = json.loads(dict(package.draft_provenance)[link.playlist_uuid])
    metadata["recovered_from_draft_uuid"] = link.playlist_uuid
    self_link = replace(package, draft_provenance=((link.playlist_uuid, json.dumps(metadata)),))
    with pytest.raises(ConfigurationExportError, match="reference itself"):
        inspect_configuration(configuration_export._encode(self_link))
    metadata = json.loads(dict(package.draft_provenance)[link.playlist_uuid])
    metadata["attribution"]["credit"] = "inconsistent credit"
    tampered = replace(package, draft_provenance=((link.playlist_uuid, json.dumps(metadata)),))
    with pytest.raises(ConfigurationExportError, match="disagrees with current artwork"):
        inspect_configuration(configuration_export._encode(tampered))


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


@pytest.mark.parametrize("location", ["owner", "draft"])
def test_restore_rejects_fold_only_difference(tmp_path, location):
    _donor(tmp_path)
    donor = ForwardOnlyManagedStateStore(tmp_path / "legacy", readonly=True)
    state = donor.load_readonly()
    sync = ConfigurationSynchronization()
    managed = state.playlists[0]
    template = next(t for t in donor.catalog.playlists if t.id == managed.template_id)
    draft = state.templates[0]

    def edit_fold(fold):
        if location == "owner":
            maximum = max(
                template.duration_policy.default_max_seconds, managed.max_duration_seconds
            )
            exception = DurationPolicyException(
                "fold_regression",
                template.default_source_ids[0],
                time(2, 30, fold=fold),
                maximum + 60,
            )
            donor.save_owner_template(
                replace(
                    template,
                    duration_policy=replace(template.duration_policy, exceptions=(exception,)),
                )
            )
        else:
            exception = DurationPolicyException(
                "fold_regression",
                draft.default_source_ids[0],
                time(2, 30, fold=fold),
                draft.max_duration_seconds + 60,
            )
            edited_draft = replace(draft, duration_exceptions=(exception,))
            donor.save(replace(state, templates=(edited_draft,)))
        return export_configuration(tmp_path / "legacy", sync)

    first = edit_fold(0)
    second = edit_fold(1)
    assert first != second
    if location == "owner":
        initial = inspect_configuration(first)
        changed = inspect_configuration(second)
        assert initial.owner_document != changed.owner_document
        mixed_package = replace(initial, owner_document=changed.owner_document)
        mixed = configuration_export._encode(mixed_package)
        with pytest.raises(ConfigurationExportError, match="association does not match"):
            inspect_configuration(mixed)
    else:
        assert (
            inspect_configuration(first).owner_document
            == inspect_configuration(second).owner_document
        )

    root, service = _receiver(tmp_path)
    preview = service.preview(first)
    service.apply(first, preview.fingerprint, confirmed=True)
    before = _files(root)
    repeated = service.preview(first)
    assert repeated.already_restored
    assert service.apply(first, repeated.fingerprint, confirmed=True).preview.already_restored
    with pytest.raises(ConfigurationRestoreError, match="overwrite"):
        service.preview(second)
    with pytest.raises(ConfigurationRestoreError, match="overwrite"):
        service.apply(second, repeated.fingerprint, confirmed=True)
    assert before == _files(root)


def test_standalone_imported_template_credit_survives_managed_export_and_restore(tmp_path):
    _donor(tmp_path, local_managed=True)
    donor = ForwardOnlyManagedStateStore(tmp_path / "legacy", readonly=True)
    state = donor.load_readonly()
    sync = ConfigurationSynchronization()
    package = inspect_configuration(export_configuration(tmp_path / "legacy", sync))
    member = next(m for m in inspect_collection(package.collection_document) if m.kind == "draft")
    bundle = inspect_playlist_bundle_v3(member.document)
    metadata = json.loads(bundle.metadata)
    metadata["attribution"]["credit"] = "Imported original artwork credit"
    draft = replace(state.templates[0], portable_metadata=json.dumps(metadata))
    donor.save(replace(state, templates=(draft,)))

    document = export_configuration(tmp_path / "legacy", sync)
    package = inspect_configuration(document)
    link = next(link for link in package.managed_links if link.template_kind == "draft")
    members = inspect_collection(package.collection_document)
    member = next(m for m in members if m.portable_id == link.playlist_uuid)
    restored_bundle = inspect_playlist_bundle_v3(member.document)
    assert restored_bundle.template_attribution == metadata["attribution"]

    _, service = _receiver(tmp_path)
    preview = service.preview(document)
    service.apply(document, preview.fingerprint, confirmed=True)
    state = service.store.load_readonly()
    restored = next(d for d in state.templates if d.portable_id == link.template_id)
    assert json.loads(restored.portable_metadata)["attribution"] == metadata["attribution"]


def test_standalone_draft_shared_owner_cover_survives_export_and_restore(tmp_path):
    # Restore real owner artwork so the inventory has a recorded credit for
    # uploaded_<sha256>, then reuse that exact JPEG in a new independent draft.
    initial = _donor(tmp_path)
    root, donor = _receiver(tmp_path)
    preview = donor.preview(initial)
    donor.apply(initial, preview.fingerprint, confirmed=True)
    state = donor.store.load_readonly()
    template = donor.store.catalog.playlist("international_english_news")
    cover_id = template.cover_id
    assert cover_id.startswith("uploaded_")
    jpeg, generic_credit = PortableCoverLoader(root).load(cover_id)
    owner_credit = donor.store.owner_cover_attribution(cover_id)
    assert owner_credit is not None and owner_credit != generic_credit

    draft = _draft(id="custom_shared_cover", portable_id=str(uuid4()), cover_id=cover_id)
    assert draft.portable_metadata is None
    assert draft.owner_template_id is None
    managed = replace(
        activate_template(draft.as_playlist_template(), "private-shared-cover"),
        portable_id=str(uuid4()),
        enabled=False,
    )
    donor.store.save(
        replace(
            state,
            templates=tuple(sorted((*state.templates, draft), key=lambda item: item.id)),
            playlists=(managed,),
        )
    )
    before = _files(root)
    document = export_configuration(root, donor.synchronization)
    assert before == _files(root)
    package = inspect_configuration(document)
    members = {
        member.portable_id: inspect_playlist_bundle_v3(member.document)
        for member in inspect_collection(package.collection_document)
    }
    assert members[draft.portable_id].jpeg == jpeg
    assert members[draft.portable_id].playlist["cover"]["attribution"] == owner_credit
    assert members[managed.portable_id].template_jpeg == jpeg
    assert members[managed.portable_id].template_attribution == owner_credit
    assert members[managed.portable_id].playlist["cover"]["attribution"] == owner_credit

    second = tmp_path / "second"
    second.mkdir()
    receiver_root, receiver = _receiver(second)
    before = _files(receiver_root)
    preview = receiver.preview(document)
    assert before == _files(receiver_root)
    receiver.apply(document, preview.fingerprint, confirmed=True)
    restored = receiver.store.load_readonly()
    assert not restored.playlists
    for identity in (draft.portable_id, managed.portable_id):
        record = next(item for item in restored.templates if item.portable_id == identity)
        assert record.cover_id == cover_id
        assert json.loads(record.portable_metadata)["attribution"] == owner_credit
    assert receiver.preview(document).already_restored
