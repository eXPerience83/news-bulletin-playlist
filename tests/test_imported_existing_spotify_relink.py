"""Offline adoption of imported ZIPs into verified EXISTING Spotify playlists.

No real Spotify access, provider mutation, SQLite, OAuth state or production /data.
"""

from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from _portable_playlist_fixtures import OTHER_UUID, donor
from _provisioning_fixtures import _request_intent

from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_admin import ManagedAdminError, ManagedAdminService
from news_bulletin_playlist.managed_admin_web import _custom_draft_card
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal
from news_bulletin_playlist.managed_state import (
    ManagedState,
    ManagedStateStore,
    activate_template,
    effective_sources,
)
from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.playlist_import import PlaylistImportService
from news_bulletin_playlist.portable_playlists import PortableCoverLoader
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError

EMPTY = BuiltInCatalog((), ())
DESTINATION = "R" * 22
USER = "owned-user"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        pytest.fail("A provider test attempted real network I/O")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


class SpotifyReadOnly:
    def __init__(
        self,
        *,
        remote_id: str = DESTINATION,
        owner: str = USER,
        name: str = "",
        error: Exception | None = None,
    ) -> None:
        self.remote_id = remote_id
        self.owner = owner
        self.name = name
        self.error = error
        self.calls: list[str] = []

    def current_user(self) -> dict[str, str]:
        self.calls.append("current_user")
        if self.error:
            raise self.error
        return {"id": USER}

    def playlist_details(self, playlist_id: str) -> dict[str, Any]:
        self.calls.append("playlist_details")
        assert playlist_id == DESTINATION
        return {
            "id": self.remote_id,
            "owner": {"id": self.owner},
            "name": self.name,
        }

    def create_playlist(self, *args: Any, **kwargs: Any) -> None:
        pytest.fail("Relink must never create a remote playlist")

    def change_playlist_details(self, *args: Any, **kwargs: Any) -> None:
        pytest.fail("Relink must never change Spotify metadata")

    def upload_playlist_cover(self, *args: Any, **kwargs: Any) -> None:
        pytest.fail("Relink must never upload Spotify artwork")


def fixture(tmp_path: Path):
    document = donor(tmp_path)
    root = tmp_path / "receiver"
    root.mkdir()
    store = ManagedStateStore(root / "managed-state.json", catalog=EMPTY)
    store.save(ManagedState(schema_version=10))
    journal = ProvisioningJournal(root / "provisioning.json")
    covers = PortableCoverLoader(root)
    importer = PlaylistImportService(store, ConfigurationSynchronization(), journal, covers)
    preview = importer.preview(document)
    assert preview.status == "create"
    applied = importer.apply(
        document,
        preview.sources.fingerprint,
        keep_local=preview.sources.conflicts,
        confirm_schema=False,
    )
    assert applied.created_draft
    draft = store.load_readonly().templates[0]
    assert draft.portable_metadata is not None
    client = SpotifyReadOnly(name=draft.display_name)
    service = ManagedAdminService(
        store,
        catalog=EMPTY,
        client_factory=lambda _token: client,
        cover_loader=lambda cover: covers.load(cover)[0],
        provisioning_journal=journal,
    )
    return service, draft, client


def relink(service: ManagedAdminService, draft, *, confirm: bool = True):
    return service.relink_imported_destination(
        template_id=draft.id,
        destination_id=DESTINATION,
        access_token="synthetic-access",
        confirm_existing=confirm,
    )


def test_relink_existing_destination_is_paused_and_never_mutates_spotify(tmp_path: Path):
    service, draft, client = fixture(tmp_path)
    before = service.store.load_readonly()
    result = relink(service, draft)
    assert result.enabled is False
    assert result.destination.external_id == DESTINATION
    assert result.source_ids == draft.default_source_ids
    assert result.cover_id == draft.cover_id
    assert result.description == draft.description
    assert result.display_name == draft.display_name
    assert result.max_duration_seconds == draft.max_duration_seconds
    assert result.min_duration_seconds == draft.min_duration_seconds
    assert result.portable_id is not None and result.portable_id != draft.portable_id
    assert client.calls == ["current_user", "playlist_details"]
    after = service.store.load_readonly()
    assert after.sources == before.sources and after.templates == before.templates
    assert after.playlists == (result,)
    assert service.provisioning_journal.load() is None
    assert service.snapshot().managed == (result,)
    assert len(client.calls) == 2
    with pytest.raises(ManagedAdminError, match="already has"):
        relink(service, draft)
    assert len(client.calls) == 2


@pytest.mark.parametrize("problem", ["no_confirm", "wrong_owner", "wrong_name", "wrong_id"])
def test_relink_rejects_unverified_destination_before_local_write(
    tmp_path: Path, problem: str
):
    service, draft, client = fixture(tmp_path)
    if problem == "wrong_owner":
        client.owner = "other-user"
    elif problem == "wrong_name":
        client.name = "A different playlist"
    elif problem == "wrong_id":
        client.remote_id = "Z" * 22
    prior = service.store.path.read_bytes()
    with pytest.raises(ManagedAdminError):
        relink(service, draft, confirm=problem != "no_confirm")
    assert service.store.path.read_bytes() == prior
    assert service.store.load_readonly().playlists == ()
    assert client.calls == ([] if problem == "no_confirm" else ["current_user", "playlist_details"])


def test_invalid_id_and_pending_creation_fail_without_provider_reads(tmp_path: Path):
    service, draft, client = fixture(tmp_path)
    with pytest.raises(ManagedAdminError, match="22-character"):
        service.relink_imported_destination(
            template_id=draft.id,
            destination_id="https://open.spotify.com/playlist/" + DESTINATION,
            access_token="synthetic-access",
            confirm_existing=True,
        )
    assert not client.calls
    service.provisioning_journal.save(_request_intent())
    with pytest.raises(ManagedAdminError):
        relink(service, draft)
    assert not client.calls


@pytest.mark.parametrize(
    "error",
    [SpotifyApiError(403, "no user scope"), SpotifyTransportError("unavailable")],
)
def test_provider_unavailable_cannot_create_local_binding(
    tmp_path: Path, error: Exception
):
    service, draft, client = fixture(tmp_path)
    client.error = error
    before = service.store.path.read_bytes()
    with pytest.raises((ManagedAdminError, SpotifyTransportError)):
        relink(service, draft)
    assert service.store.path.read_bytes() == before
    assert client.calls == ["current_user"]


def test_relink_rejects_duplicate_disabled_destination_before_remote_read(tmp_path: Path):
    service, draft, client = fixture(tmp_path)
    state = service.store.load_readonly()
    other = replace(
        draft,
        id=PlaylistId("custom_different"),
        portable_metadata=None,
        portable_id=OTHER_UUID,
    )
    occupied = replace(
        activate_template(other.as_playlist_template(), DESTINATION),
        enabled=False,
        portable_id=None,
    )
    service.store.save(
        replace(state, templates=(draft, other), playlists=(occupied,))
    )
    with pytest.raises(ManagedAdminError, match="already locally managed"):
        relink(service, draft)
    assert not client.calls


def test_relink_retains_dormant_source_assignments_and_allows_paused_state(tmp_path: Path):
    service, draft, client = fixture(tmp_path)
    state = service.store.load_readonly()
    service.store.save(
        replace(state, sources=tuple(replace(s, enabled=False) for s in state.sources))
    )
    result = relink(service, draft)
    assert result.source_ids == draft.default_source_ids
    assert result.enabled is False
    assert not any(s.enabled for s in service.store.load_readonly().sources)
    assert client.calls == ["current_user", "playlist_details"]


def test_only_imported_draft_may_relink_and_admin_form_is_explicit(tmp_path: Path):
    service, draft, client = fixture(tmp_path)
    sources = effective_sources(EMPTY, service.store.load_readonly())
    page = _custom_draft_card(
        draft,
        catalog=EMPTY,
        csrf_token="synthetic-csrf",
        sources=sources,
        custom_source_ids=frozenset(source.id for source in sources),
        spotify_connected=True,
        managed=(),
        provisioning_intent=None,
    )
    assert 'action="/admin/playlists/relink"' in page
    assert 'name="confirm_existing" value="1" required' in page
    assert 'pattern="[A-Za-z0-9]{22}"' in page
    plain = replace(
        draft,
        id=PlaylistId("custom_plain"),
        portable_metadata=None,
        portable_id=OTHER_UUID,
    )
    state = service.store.load_readonly()
    service.store.save(replace(state, templates=(draft, plain)))
    with pytest.raises(ManagedAdminError, match="Only imported"):
        service.relink_imported_destination(
            template_id=plain.id,
            destination_id=DESTINATION,
            access_token="synthetic-access",
            confirm_existing=True,
        )
    assert not client.calls
