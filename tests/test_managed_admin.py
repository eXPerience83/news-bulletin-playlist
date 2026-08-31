from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.managed_admin import (
    ManagedAdminError,
    ManagedAdminService,
    SpotifyPlaylistPersistenceError,
    SpotifyPlaylistProvisioningError,
)
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.models import CountryCode, LanguageTag, PlaylistId, SourceId


class _FakeSpotifyClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.create_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, str, str]] = []

    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:
        self.create_calls.append((name, description))
        return self.responses.pop(0)

    def change_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        self.update_calls.append((playlist_id, name, description))
        return {}


class _Factory:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.client = _FakeSpotifyClient(responses)
        self.tokens: list[str] = []

    def __call__(self, access_token: str) -> _FakeSpotifyClient:
        self.tokens.append(access_token)
        return self.client


class _FailingSaveStore(ManagedStateStore):
    def __init__(self, path: Path, *, fail_after: int = 0) -> None:
        super().__init__(path)
        self.fail_after = fail_after
        self.saves = 0

    def save(self, state: object) -> None:
        if self.saves >= self.fail_after:
            raise OSError("disk unavailable")
        self.saves += 1
        super().save(state)  # type: ignore[arg-type]


def _service(
    tmp_path: Path,
    responses: list[dict[str, Any]],
) -> tuple[ManagedAdminService, _Factory]:
    factory = _Factory(responses)
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=factory,
    )
    return service, factory


def test_snapshot_exposes_unmanaged_builtin_template(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, [])

    snapshot = service.snapshot()

    assert snapshot.managed == ()
    assert [template.display_name for template in snapshot.available_templates] == [
        "Noticias España"
    ]


def test_activate_creates_one_private_destination_and_persists_explicit_choices(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path, [{"id": "spotify-destination"}])

    managed = service.activate(
        template_id="spain_spanish_news",
        display_name=" Noticias España ",
        description="Descripción personalizada",
        cover_id="spain_spanish_news",
        source_ids=("ser", "cnn"),
        access_token="access-token-sentinel",
    )

    assert factory.tokens == ["access-token-sentinel"]
    assert factory.client.create_calls == [("Noticias España", "Descripción personalizada")]
    assert managed.destination.external_id == "spotify-destination"
    assert managed.source_ids == (SourceId("ser"), SourceId("cnn"))
    snapshot = service.snapshot()
    assert snapshot.managed == (managed,)
    assert snapshot.available_templates == ()


def test_activate_rejects_unknown_source_before_spotify_write(tmp_path: Path) -> None:
    service, factory = _service(tmp_path, [{"id": "must-not-be-used"}])

    with pytest.raises(ManagedAdminError, match="unknown catalog source"):
        service.activate(
            template_id="spain_spanish_news",
            display_name="Noticias España",
            description="test",
            cover_id="spain_spanish_news",
            source_ids=("missing",),
            access_token="token",
        )

    assert factory.tokens == []
    assert factory.client.create_calls == []


def test_activate_same_template_twice_does_not_create_duplicate_spotify_playlist(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path, [{"id": "first"}, {"id": "must-not-be-used"}])
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    with pytest.raises(ManagedAdminError, match="already managed"):
        service.activate(
            template_id=template.id,
            display_name=template.display_name,
            description=template.description,
            cover_id=template.cover_id,
            source_ids=template.default_source_ids,
            access_token="token",
        )

    assert factory.client.create_calls == [(template.display_name, template.description)]


def test_spotify_creation_persistence_failure_surfaces_recoverable_destination_id(
    tmp_path: Path,
) -> None:
    factory = _Factory([{"id": "created-but-not-saved"}])
    service = ManagedAdminService(
        _FailingSaveStore(tmp_path / "managed-state.json"),
        client_factory=factory,
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")

    with pytest.raises(SpotifyPlaylistProvisioningError) as raised:
        service.activate(
            template_id=template.id,
            display_name=template.display_name,
            description=template.description,
            cover_id=template.cover_id,
            source_ids=template.default_source_ids,
            access_token="token",
        )

    assert raised.value.playlist_id == "created-but-not-saved"
    assert "created-but-not-saved" in str(raised.value)
    assert factory.client.create_calls == [(template.display_name, template.description)]


def test_update_synchronizes_name_and_description_to_spotify(tmp_path: Path) -> None:
    service, factory = _service(tmp_path, [{"id": "destination"}])
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="create-token",
    )

    updated = service.update(
        managed.id,
        display_name="Noticias España Ahora",
        description="Descripción nueva",
        cover_id=managed.cover_id,
        source_ids=("ser", "cnn"),
        enabled=True,
        access_token="update-token",
    )

    assert updated.display_name == "Noticias España Ahora"
    assert factory.tokens == ["create-token", "update-token"]
    assert factory.client.update_calls == [
        ("destination", "Noticias España Ahora", "Descripción nueva")
    ]


def test_source_only_update_does_not_write_spotify_metadata(tmp_path: Path) -> None:
    service, factory = _service(tmp_path, [{"id": "destination"}])
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="create-token",
    )

    service.update(
        managed.id,
        display_name=managed.display_name,
        description=managed.description,
        cover_id=managed.cover_id,
        source_ids=("rne",),
        enabled=True,
        access_token="unused-update-token",
    )

    assert factory.client.update_calls == []
    assert factory.tokens == ["create-token"]


def test_metadata_persistence_failure_surfaces_destination_for_reconciliation(
    tmp_path: Path,
) -> None:
    factory = _Factory([{"id": "destination"}])
    store = _FailingSaveStore(tmp_path / "managed-state.json", fail_after=1)
    service = ManagedAdminService(store, client_factory=factory)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="create-token",
    )

    with pytest.raises(SpotifyPlaylistPersistenceError) as raised:
        service.update(
            managed.id,
            display_name="Nuevo nombre",
            description=managed.description,
            cover_id=managed.cover_id,
            source_ids=managed.source_ids,
            enabled=True,
            access_token="update-token",
        )

    assert raised.value.playlist_id == "destination"
    assert factory.client.update_calls == [
        ("destination", "Nuevo nombre", managed.description)
    ]


def test_paused_playlist_may_have_no_sources_but_cannot_resume_until_sources_selected(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path, [{"id": "destination"}])
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    paused = service.update(
        managed.id,
        display_name="Noticias España",
        description="En pausa",
        cover_id=managed.cover_id,
        source_ids=(),
        enabled=False,
        access_token="update-token",
    )

    assert paused.enabled is False
    assert paused.source_ids == ()
    with pytest.raises(ManagedAdminError, match="resumed playlist"):
        service.set_enabled(managed.id, True)


def test_editing_one_playlist_does_not_change_source_membership_of_another(
    tmp_path: Path,
) -> None:
    first_template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    second_template = PlaylistTemplate(
        id=PlaylistId("world_spanish_news"),
        display_name="Noticias Mundo",
        description="Actualidad internacional en español.",
        countries=(CountryCode("US"),),
        languages=(LanguageTag("es"),),
        default_source_ids=(SourceId("ser"), SourceId("cnn")),
        cover_id="international_spanish_news",
    )
    catalog = BuiltInCatalog(
        sources=BUILTIN_CATALOG.sources,
        playlists=(first_template, second_template),
    )
    factory = _Factory([{"id": "spain"}, {"id": "world"}])
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        catalog=catalog,
        client_factory=factory,
    )
    first = service.activate(
        template_id=first_template.id,
        display_name=first_template.display_name,
        description=first_template.description,
        cover_id=first_template.cover_id,
        source_ids=("ser", "cnn"),
        access_token="token",
    )
    second = service.activate(
        template_id=second_template.id,
        display_name=second_template.display_name,
        description=second_template.description,
        cover_id=second_template.cover_id,
        source_ids=("ser", "cnn"),
        access_token="token",
    )

    service.update(
        first.id,
        display_name=first.display_name,
        description=first.description,
        cover_id=first.cover_id,
        source_ids=("rne",),
        enabled=True,
        access_token="unused-update-token",
    )

    snapshot = service.snapshot()
    by_id = {playlist.id: playlist for playlist in snapshot.managed}
    assert by_id[first.id].source_ids == (SourceId("rne"),)
    assert by_id[second.id].source_ids == (SourceId("ser"), SourceId("cnn"))


def test_stop_managing_removes_local_instance_without_spotify_delete(tmp_path: Path) -> None:
    service, factory = _service(tmp_path, [{"id": "destination-to-keep"}])
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    destination = service.stop_managing(managed.id)

    assert destination == "destination-to-keep"
    assert service.snapshot().managed == ()
    assert service.snapshot().available_templates == (template,)
    assert factory.client.create_calls == [(template.display_name, template.description)]
    assert factory.client.update_calls == []
