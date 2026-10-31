"""Quarantined v7 drafts preserve bad data while healthy destinations remain eligible."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.effective_config import load_effective_config
from news_bulletin_playlist.managed_admin import ManagedAdminError, ManagedAdminService
from news_bulletin_playlist.managed_admin_web import render_managed_admin_page
from news_bulletin_playlist.managed_provisioning import (
    ProvisioningIntent,
    ProvisioningJournal,
    ProvisioningState,
)
from news_bulletin_playlist.managed_state import (
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    activate_template,
    parse_managed_state,
    serialize_managed_state,
)
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.spotify.auth import AuthorizationState
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME


def _draft(id_value: str) -> LocalPlaylistTemplate:
    return LocalPlaylistTemplate(
        id=PlaylistId(id_value),
        display_name="Safe local draft",
        description="Safe description",
        default_source_ids=(SourceId("cnn"),),
        cover_id="international_spanish_news",
    )


def _state() -> ManagedState:
    keys = (
        "spain_spanish_news",
        "international_english_news",
        "international_polish_news",
        "international_spanish_long_news",
    )
    return ManagedState(
        schema_version=7,
        templates=(_draft("custom_safe"), _draft("custom_broken")),
        playlists=tuple(
            activate_template(BUILTIN_CATALOG.playlist(key), f"existing-destination-{i}")
            for i, key in enumerate(keys)
        ),
    )


def _tamper(path: Path, *, broken_id: str = "custom_broken") -> bytes:
    payload = json.loads(path.read_bytes())
    for raw in payload["templates"]:
        if raw["id"] == broken_id:
            raw["max_duration_seconds"] = -123
            raw["description"] = "DO-NOT-EXPOSE-IN-ADMIN"
            break
    raw_bytes = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(raw_bytes)
    return raw_bytes


def test_invalid_unreferenced_draft_does_not_stop_four_existing_destinations(
    tmp_path: Path,
) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    original = _state()
    store.save(original)
    tampered_bytes = _tamper(store.path)

    loaded = store.load()
    assert store.path.read_bytes() == tampered_bytes  # never repairs on read
    assert loaded.templates == (_draft("custom_safe"),)
    assert len(loaded.quarantined_templates) == 1
    assert loaded.quarantined_templates[0].template_id == "custom_broken"
    assert json.loads(loaded.quarantined_templates[0].raw_json)["max_duration_seconds"] == -123
    assert loaded.playlists == original.playlists

    config = load_effective_config(tmp_path, {})
    assert config is not None
    assert len(config.playlists) == 4
    assert {item.destination.external_id for item in config.playlists} == {
        item.destination.external_id for item in original.playlists
    }

    # V11 is authoritative after first runtime load; preserve the original
    # tampered bytes as inert evidence while retaining quarantined data.
    owner = open_managed_state_store(tmp_path)
    ManagedAdminService(owner).stop_managing("spain_spanish_news")
    after = owner.load()
    assert store.path.read_bytes() == tampered_bytes
    assert len(after.quarantined_templates) == 1
    assert json.loads(after.quarantined_templates[0].raw_json)["description"] == (
        "DO-NOT-EXPOSE-IN-ADMIN"
    )
    assert len(after.playlists) == 3
    assert (
        json.loads((tmp_path / V11_OPERATIONAL_FILENAME).read_bytes())["managed_overlay"][
            "quarantined_templates"
        ][0]["raw"]["max_duration_seconds"]
        == -123
    )
    assert owner.load() == after


def test_invalid_referenced_custom_draft_suspends_only_its_destination(tmp_path: Path) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    state = _state()
    existing = activate_template(_draft("custom_broken").as_playlist_template(), "A" * 22)
    store.save(replace(state, playlists=state.playlists + (existing,)))
    _tamper(store.path)
    loaded = store.load()
    assert len(loaded.playlists) == 5
    assert len(loaded.quarantined_templates) == 1

    config = load_effective_config(tmp_path, {})
    assert config is not None
    assert len(config.playlists) == 4
    assert "A" * 22 not in {item.destination.external_id for item in config.playlists}
    owner = open_managed_state_store(tmp_path)
    assert len(owner.load().playlists) == 5  # never removes managed destination

    service = ManagedAdminService(owner)
    with pytest.raises(ManagedAdminError, match="quarantined"):
        service.stop_managing(existing.id)
    with pytest.raises(ManagedAdminError, match="quarantined"):
        service._managed(owner.load(), existing.id)

    page = render_managed_admin_page(
        snapshot=service.snapshot(),
        catalog=BUILTIN_CATALOG,
        spotify_state=AuthorizationState.CONNECTED,
        csrf_token="token",
        last_cycle=None,
        lan_mode=False,
        state=owner.load(),
    ).decode()
    assert "Custom playlist drafts require repair" in page
    assert "Suspended" in page
    assert "DO-NOT-EXPOSE-IN-ADMIN" not in page
    assert page.count('action="/admin/playlists/sync"') == 4


def test_corrupt_pending_known_destination_is_retained_for_recovery(tmp_path: Path) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    store.save(_state())
    _tamper(store.path)
    intent = ProvisioningIntent(
        state=ProvisioningState.DESTINATION_KNOWN,
        template_id=PlaylistId("custom_broken"),
        display_name="Custom destination",
        description="Example",
        cover_id="international_spanish_news",
        source_ids=(SourceId("cnn"),),
        retention_hours=48,
        max_episodes=100,
        max_duration_seconds=1800,
        destination_id="B" * 22,
    )
    journal = ProvisioningJournal(tmp_path / "managed-playlist-provisioning.json")
    journal.save(intent)
    service = ManagedAdminService(store)

    assert service.finalize_known_provisioning() is None
    assert journal.load() == intent
    assert store.load().playlists == _state().playlists


def test_duplicate_draft_ids_do_not_select_arbitrary_candidate(tmp_path: Path) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    store.save(_state())
    payload = json.loads(store.path.read_bytes())
    payload["templates"].append(
        dict(next(item for item in payload["templates"] if item["id"] == "custom_safe"))
    )
    loaded = parse_managed_state(payload)
    assert [item.id for item in loaded.templates] == [PlaylistId("custom_broken")]
    assert len(loaded.quarantined_templates) == 2
    assert all(item.template_id == "custom_safe" for item in loaded.quarantined_templates)
    assert all(item.reason == "duplicate_id" for item in loaded.quarantined_templates)


def test_bad_root_document_still_fails_closed(tmp_path: Path) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    store.save(_state())
    root = json.loads(store.path.read_bytes())
    root["source_overrides"] = [{"source_id": "nonexistent", "public_release_mode": "auto"}]
    with pytest.raises(ManagedStateError):
        parse_managed_state(root)
    root = json.loads(store.path.read_bytes())
    root["mystery_root_field"] = []
    with pytest.raises(ManagedStateError, match="unknown key"):
        parse_managed_state(root)


def test_quarantined_unreferenced_draft_blocks_automatic_source_reference_loss(
    tmp_path: Path,
) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    store.save(_state())
    _tamper(store.path)
    state = store.load()
    assert state.quarantined_templates
    assert len(serialize_managed_state(state)["quarantined_templates"]) == 1


def test_backup_confirmed_repair_discards_only_explicit_unreferenced_record(
    tmp_path: Path,
) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    store.save(_state())
    _tamper(store.path)
    service = ManagedAdminService(store)
    damaged = store.load().quarantined_templates[0]
    digest = hashlib.sha256(damaged.raw_json.encode("utf-8")).hexdigest()
    before = store.path.read_bytes()

    with pytest.raises(ManagedAdminError, match="backup"):
        service.discard_quarantined_template(0, digest, backup_confirmed=False)
    with pytest.raises(ManagedAdminError, match="changed"):
        service.discard_quarantined_template(0, "0" * 64, backup_confirmed=True)
    with pytest.raises(ManagedAdminError, match="no longer exists"):
        service.discard_quarantined_template(8, digest, backup_confirmed=True)
    assert store.path.read_bytes() == before

    service.discard_quarantined_template(0, digest, backup_confirmed=True)
    after = store.load()
    assert after.quarantined_templates == ()
    assert after.templates == (_draft("custom_safe"),)
    assert after.playlists == _state().playlists
    assert "DO-NOT-EXPOSE-IN-ADMIN" not in store.path.read_text()
    assert len(load_effective_config(tmp_path, {}).playlists) == 4


def test_cannot_discard_draft_needed_by_suspended_destination(tmp_path: Path) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    draft = _draft("custom_broken")
    playlist = activate_template(draft.as_playlist_template(), "C" * 22)
    state = replace(_state(), playlists=_state().playlists + (playlist,))
    store.save(state)
    original = _tamper(store.path)
    damaged = store.load().quarantined_templates[0]
    digest = hashlib.sha256(damaged.raw_json.encode("utf-8")).hexdigest()

    with pytest.raises(ManagedAdminError, match="published custom playlist"):
        ManagedAdminService(store).discard_quarantined_template(0, digest, backup_confirmed=True)
    assert store.path.read_bytes() == original


def test_missing_draft_id_still_suspends_unresolved_custom_destination(tmp_path: Path) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    local = _draft("custom_broken")
    selected = activate_template(local.as_playlist_template(), "D" * 22)
    store.save(replace(_state(), playlists=_state().playlists + (selected,)))
    payload = json.loads(store.path.read_bytes())
    for draft in payload["templates"]:
        if draft["id"] == "custom_broken":
            draft.pop("id")
    store.path.write_text(json.dumps(payload))
    state = store.load()
    assert len(state.quarantined_templates) == 1
    assert state.quarantined_templates[0].template_id is None

    config = load_effective_config(tmp_path, {})
    assert config is not None
    assert len(config.playlists) == 4
    assert "D" * 22 not in {playlist.destination.external_id for playlist in config.playlists}
    owner = open_managed_state_store(tmp_path)
    assert len(owner.load().playlists) == 5
    with pytest.raises(ManagedAdminError, match="quarantined"):
        ManagedAdminService(owner).stop_managing(selected.id)


def test_pending_provisioning_blocks_quarantine_discard(tmp_path: Path) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    store.save(_state())
    _tamper(store.path)
    service = ManagedAdminService(store)
    item = store.load().quarantined_templates[0]
    digest = hashlib.sha256(item.raw_json.encode("utf-8")).hexdigest()
    intent = ProvisioningIntent(
        state=ProvisioningState.DESTINATION_KNOWN,
        template_id=PlaylistId("custom_broken"),
        display_name="Unknown destination",
        description="Unresolved creation",
        cover_id="international_spanish_news",
        source_ids=(SourceId("cnn"),),
        retention_hours=48,
        max_episodes=100,
        max_duration_seconds=1800,
        destination_id="F" * 22,
    )
    service.provisioning_journal.save(intent)
    with pytest.raises(ManagedAdminError, match="pending Spotify provisioning"):
        service.discard_quarantined_template(0, digest, backup_confirmed=True)
    assert service.provisioning_journal.load() == intent
    assert store.load().quarantined_templates == (item,)
