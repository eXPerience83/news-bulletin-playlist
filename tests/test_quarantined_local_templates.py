"""Quarantined v7 drafts preserve bad data while healthy destinations remain eligible."""

from __future__ import annotations

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

    # A routine local save must retain the invalid data (in quarantine), not drop it.
    ManagedAdminService(store).stop_managing("spain_spanish_news")
    after = store.load()
    assert len(after.quarantined_templates) == 1
    assert json.loads(after.quarantined_templates[0].raw_json)["description"] == (
        "DO-NOT-EXPOSE-IN-ADMIN"
    )
    assert len(after.playlists) == 3
    assert json.loads(store.path.read_bytes())["quarantined_templates"][0]["raw"][
        "max_duration_seconds"
    ] == -123
    assert store.load() == after


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
    assert len(store.load().playlists) == 5  # never removes managed destination

    service = ManagedAdminService(store)
    with pytest.raises(ManagedAdminError, match="quarantined"):
        service.stop_managing(existing.id)
    with pytest.raises(ManagedAdminError, match="quarantined"):
        service._managed(store.load(), existing.id)

    page = render_managed_admin_page(
        snapshot=service.snapshot(),
        catalog=BUILTIN_CATALOG,
        spotify_state=AuthorizationState.CONNECTED,
        csrf_token="token",
        last_cycle=None,
        lan_mode=False,
        state=store.load(),
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
