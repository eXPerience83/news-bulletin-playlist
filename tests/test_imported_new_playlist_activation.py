"""Imported ZIP activation is a separate, explicit NEW Spotify creation workflow.

All input files are synthetic. No network, OAuth or live Spotify state is used.
The old destination ID is intentionally never read or adopted.
"""

from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from _portable_playlist_fixtures import donor

from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_admin import (
    ManagedAdminError,
    ManagedAdminService,
    SpotifyPlaylistCreationUncertainError,
    render_spotify_description,
)
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal, ProvisioningState
from news_bulletin_playlist.managed_state import ManagedState, ManagedStateStore
from news_bulletin_playlist.playlist_import import PlaylistImportService
from news_bulletin_playlist.portable_playlists import PortableCoverLoader
from news_bulletin_playlist.spotify.client import SpotifyTransportError

EMPTY_CATALOG = BuiltInCatalog((), ())
NEW_DESTINATION = "N" * 22


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        pytest.fail("Imported playlist tests must never access a network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


class FakeSpotify:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.creates: list[tuple[str, str]] = []
        self.covers: list[tuple[str, bytes]] = []

    def create_playlist(
        self, name: str, *, public: bool = True, description: str = ""
    ) -> dict[str, str]:
        assert public
        self.creates.append((name, description))
        if self.error is not None:
            raise self.error
        return {"id": NEW_DESTINATION}

    def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        self.covers.append((playlist_id, jpeg_bytes))
        return {}


def setup_import(tmp_path: Path, client: FakeSpotify):
    archive = donor(tmp_path)
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    store = ManagedStateStore(receiver / "managed-state.json", catalog=EMPTY_CATALOG)
    store.save(ManagedState(schema_version=10))
    journal = ProvisioningJournal(receiver / "provisioning.json")
    covers = PortableCoverLoader(receiver)
    importer = PlaylistImportService(store, ConfigurationSynchronization(), journal, covers)
    preview = importer.preview(archive)
    assert preview.status == "create"
    result = importer.apply(
        archive,
        preview.sources.fingerprint,
        keep_local=preview.sources.conflicts,
        confirm_schema=False,
    )
    assert result.created_draft
    draft = store.load_readonly().templates[0]
    assert draft.portable_metadata is not None
    service = ManagedAdminService(
        store,
        catalog=EMPTY_CATALOG,
        client_factory=lambda _token: client,
        cover_loader=lambda cover_id: covers.load(cover_id)[0],
        provisioning_journal=journal,
    )
    return service, draft, covers


def publish(service, draft, *, confirm: bool):
    return service.activate(
        template_id=draft.id,
        display_name=draft.display_name,
        description=draft.description,
        cover_id=draft.cover_id,
        source_ids=draft.default_source_ids,
        access_token="synthetic-token",
        min_duration_seconds=draft.min_duration_seconds,
        max_duration_seconds=draft.max_duration_seconds,
        confirm_imported_new=confirm,
    )


def test_imported_zip_requires_explicit_new_destination_confirmation(tmp_path: Path) -> None:
    client = FakeSpotify()
    service, draft, _ = setup_import(tmp_path, client)
    with pytest.raises(ManagedAdminError, match="explicit confirmation"):
        service.check_activation_allowed(str(draft.id))
    with pytest.raises(ManagedAdminError, match="requires confirmation"):
        publish(service, draft, confirm=False)
    assert not client.creates
    assert service.provisioning_journal.load() is None
    assert not service.store.load_readonly().playlists


def test_imported_zip_creates_one_new_destination_without_relinking(tmp_path: Path) -> None:
    client = FakeSpotify()
    service, draft, covers = setup_import(tmp_path, client)
    service.check_activation_allowed(str(draft.id), confirm_imported_new=True)
    managed = publish(service, draft, confirm=True)
    assert managed.destination.external_id == NEW_DESTINATION
    assert managed.template_id == draft.id
    assert managed.source_ids == draft.default_source_ids
    assert managed.min_duration_seconds == draft.min_duration_seconds
    assert managed.max_duration_seconds == draft.max_duration_seconds
    assert client.creates == [(draft.display_name, render_spotify_description(draft.description))]
    assert client.covers == [(NEW_DESTINATION, covers.load(draft.cover_id)[0])]
    assert service.provisioning_journal.load() is None
    assert len(service.store.load_readonly().playlists) == 1
    with pytest.raises(ManagedAdminError, match="already managed"):
        publish(service, draft, confirm=True)
    assert len(client.creates) == 1


@pytest.mark.parametrize("field", ["name", "source_ids", "duration"])
def test_imported_zip_rejects_unreviewed_configuration_before_journaling(
    tmp_path: Path, field: str
) -> None:
    client = FakeSpotify()
    service, draft, _ = setup_import(tmp_path, client)
    args: dict[str, Any] = {
        "template_id": draft.id,
        "display_name": draft.display_name,
        "description": draft.description,
        "cover_id": draft.cover_id,
        "source_ids": draft.default_source_ids,
        "access_token": "synthetic-token",
        "min_duration_seconds": draft.min_duration_seconds,
        "max_duration_seconds": draft.max_duration_seconds,
        "confirm_imported_new": True,
    }
    if field == "name":
        args["display_name"] = "Unreviewed name"
    elif field == "source_ids":
        args["source_ids"] = tuple(reversed(draft.default_source_ids)) + ("unknown",)
    else:
        args["max_duration_seconds"] = draft.max_duration_seconds - 60
    with pytest.raises(ManagedAdminError, match="match the saved draft exactly"):
        service.activate(**args)
    assert not client.creates
    assert service.provisioning_journal.load() is None


def test_ambiguous_new_spotify_creation_keeps_journal_and_never_retries(
    tmp_path: Path,
) -> None:
    client = FakeSpotify(error=SpotifyTransportError("synthetic uncertain write"))
    service, draft, _ = setup_import(tmp_path, client)
    with pytest.raises(SpotifyPlaylistCreationUncertainError):
        publish(service, draft, confirm=True)
    intent = service.provisioning_journal.load()
    assert intent is not None and intent.state is ProvisioningState.REQUEST_STARTED
    with pytest.raises(ManagedAdminError):
        publish(service, draft, confirm=True)
    assert len(client.creates) == 1


def test_imported_activation_retains_dormant_sources(tmp_path: Path) -> None:
    client = FakeSpotify()
    service, draft, _ = setup_import(tmp_path, client)
    state = service.store.load_readonly()
    assert len(state.sources) > 1
    dormant = replace(state.sources[0], enabled=False)
    service.store.save(replace(state, sources=(dormant, *state.sources[1:])))
    published = publish(service, draft, confirm=True)
    assert published.source_ids == draft.default_source_ids
    assert dormant.id in published.source_ids
    assert service.store.load_readonly().sources[0].enabled is False
    assert len(client.creates) == 1


def test_imported_activation_with_every_source_disabled_fails_before_provider(
    tmp_path: Path,
) -> None:
    client = FakeSpotify()
    service, draft, _ = setup_import(tmp_path, client)
    state = service.store.load_readonly()
    service.store.save(
        replace(state, sources=tuple(replace(source, enabled=False) for source in state.sources))
    )
    with pytest.raises(ManagedAdminError, match="at least one enabled source"):
        publish(service, draft, confirm=True)
    assert not client.creates
    assert service.provisioning_journal.load() is None



def test_imported_multiline_description_accepts_browser_crlf_without_changing_bytes(
    tmp_path: Path,
) -> None:
    client = FakeSpotify()
    service, draft, _covers = setup_import(tmp_path, client)
    state = service.store.load_readonly()
    reviewed = replace(draft, description="First line\nSecond line")
    service.store.save(replace(state, templates=(reviewed,)))
    managed = service.activate(
        template_id=reviewed.id,
        display_name=reviewed.display_name,
        description="First line\r\nSecond line",
        cover_id=reviewed.cover_id,
        source_ids=reviewed.default_source_ids,
        access_token="synthetic-token",
        min_duration_seconds=reviewed.min_duration_seconds,
        max_duration_seconds=reviewed.max_duration_seconds,
        confirm_imported_new=True,
    )
    assert managed.description == reviewed.description
    assert client.creates == [
        (reviewed.display_name, render_spotify_description(reviewed.description))
    ]
    assert service.provisioning_journal.load() is None
