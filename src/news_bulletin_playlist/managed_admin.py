"""Application service for Web-managed playlist instances."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.managed_state import (
    ManagedPlaylist,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.spotify.client import SpotifyClient


class ManagedAdminError(RuntimeError):
    """Safe operator-facing error for managed playlist administration."""


class SpotifyPlaylistProvisioningError(ManagedAdminError):
    """Report a created Spotify destination when local persistence did not complete."""

    def __init__(self, playlist_id: str) -> None:
        super().__init__(
            "Spotify playlist was created but local managed state could not be saved; "
            f"preserve destination id {playlist_id!r} and link it manually before retrying"
        )
        self.playlist_id = playlist_id


class SpotifyPlaylistPersistenceError(ManagedAdminError):
    """Report remote metadata success followed by a local persistence failure."""

    def __init__(self, playlist_id: str) -> None:
        super().__init__(
            "Spotify playlist metadata was updated but local managed state could not be saved; "
            f"destination id {playlist_id!r} now requires operator reconciliation"
        )
        self.playlist_id = playlist_id


class PlaylistProvisioningClient(Protocol):
    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]: ...

    def change_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]: ...


PlaylistClientFactory = Callable[[str], PlaylistProvisioningClient]


@dataclass(frozen=True, slots=True)
class ManagedAdminSnapshot:
    managed: tuple[ManagedPlaylist, ...]
    available_templates: tuple[PlaylistTemplate, ...]


class ManagedAdminService:
    """Mutate installation-owned choices while keeping the built-in catalog immutable."""

    def __init__(
        self,
        store: ManagedStateStore,
        *,
        catalog: BuiltInCatalog = BUILTIN_CATALOG,
        client_factory: PlaylistClientFactory | None = None,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.client_factory = client_factory or SpotifyClient

    def snapshot(self) -> ManagedAdminSnapshot:
        state = self.store.load()
        managed_templates = {playlist.template_id for playlist in state.playlists}
        available = tuple(
            template
            for template in self.catalog.playlists
            if template.id not in managed_templates
        )
        return ManagedAdminSnapshot(managed=state.playlists, available_templates=available)

    def activate(
        self,
        *,
        template_id: PlaylistId | str,
        display_name: str,
        description: str,
        cover_id: str,
        source_ids: Sequence[SourceId | str],
        access_token: str,
    ) -> ManagedPlaylist:
        state = self.store.load()
        template = self._template(template_id)
        if any(playlist.template_id == template.id for playlist in state.playlists):
            raise ManagedAdminError(f"playlist template {template.id} is already managed")
        selected_sources = self._source_ids(source_ids, allow_empty=False)
        name = _required_text(display_name, "playlist name")
        cover = _required_text(cover_id, "cover id")

        response = self.client_factory(access_token).create_private_playlist(
            name,
            description=description,
        )
        destination_id = _spotify_playlist_id(response)
        managed = replace(
            activate_template(template, destination_id),
            display_name=name,
            description=description,
            cover_id=cover,
            source_ids=selected_sources,
        )
        next_state = ManagedState(
            schema_version=state.schema_version,
            playlists=state.playlists + (managed,),
        )
        try:
            self._save_validated(next_state)
        except (ManagedStateError, OSError) as exc:
            raise SpotifyPlaylistProvisioningError(destination_id) from exc
        return managed

    def update(
        self,
        playlist_id: PlaylistId | str,
        *,
        display_name: str,
        description: str,
        cover_id: str,
        source_ids: Sequence[SourceId | str],
        enabled: bool,
        access_token: str,
    ) -> ManagedPlaylist:
        state = self.store.load()
        current = self._managed(state, playlist_id)
        selected_sources = self._source_ids(source_ids, allow_empty=not enabled)
        updated = replace(
            current,
            enabled=enabled,
            display_name=_required_text(display_name, "playlist name"),
            description=description,
            cover_id=_required_text(cover_id, "cover id"),
            source_ids=selected_sources,
        )
        next_state = self._state_with_replacement(state, updated)
        self._validate_state(next_state)

        metadata_changed = (
            updated.display_name != current.display_name
            or updated.description != current.description
        )
        if metadata_changed:
            self.client_factory(access_token).change_playlist_details(
                current.destination.external_id,
                name=updated.display_name,
                description=updated.description,
            )
        try:
            self.store.save(next_state)
        except (ManagedStateError, OSError) as exc:
            if metadata_changed:
                raise SpotifyPlaylistPersistenceError(
                    current.destination.external_id
                ) from exc
            raise
        return updated

    def set_enabled(self, playlist_id: PlaylistId | str, enabled: bool) -> ManagedPlaylist:
        state = self.store.load()
        current = self._managed(state, playlist_id)
        if enabled and not current.source_ids:
            raise ManagedAdminError("a resumed playlist must select at least one source")
        updated = replace(current, enabled=enabled)
        self._replace(state, updated)
        return updated

    def stop_managing(self, playlist_id: PlaylistId | str) -> str:
        state = self.store.load()
        current = self._managed(state, playlist_id)
        remaining = tuple(playlist for playlist in state.playlists if playlist.id != current.id)
        self._save_validated(
            ManagedState(schema_version=state.schema_version, playlists=remaining)
        )
        return current.destination.external_id

    def _replace(self, state: ManagedState, updated: ManagedPlaylist) -> None:
        self._save_validated(self._state_with_replacement(state, updated))

    def _state_with_replacement(
        self,
        state: ManagedState,
        updated: ManagedPlaylist,
    ) -> ManagedState:
        playlists = tuple(
            updated if playlist.id == updated.id else playlist
            for playlist in state.playlists
        )
        return ManagedState(schema_version=state.schema_version, playlists=playlists)

    def _validate_state(self, state: ManagedState) -> None:
        compile_engine_config(self.catalog, state)

    def _save_validated(self, state: ManagedState) -> None:
        # Cross-check catalog references before replacing durable state. The state-store parser
        # intentionally does not know the image-shipped catalog.
        self._validate_state(state)
        self.store.save(state)

    def _template(self, template_id: PlaylistId | str) -> PlaylistTemplate:
        try:
            return self.catalog.playlist(template_id)
        except KeyError as exc:
            raise ManagedAdminError(str(exc)) from exc

    def _managed(self, state: ManagedState, playlist_id: PlaylistId | str) -> ManagedPlaylist:
        for playlist in state.playlists:
            if playlist.id == playlist_id:
                return playlist
        raise ManagedAdminError(f"unknown managed playlist: {playlist_id}")

    def _source_ids(
        self,
        source_ids: Sequence[SourceId | str],
        *,
        allow_empty: bool,
    ) -> tuple[SourceId, ...]:
        selected = tuple(SourceId(str(source_id).strip()) for source_id in source_ids)
        if not selected and not allow_empty:
            raise ManagedAdminError("at least one source must be selected")
        if any(not str(source_id).strip() for source_id in selected):
            raise ManagedAdminError("source id must not be empty")
        if len(selected) != len(set(selected)):
            raise ManagedAdminError("source selection contains duplicates")
        known = {source.id for source in self.catalog.sources}
        unknown = next((source_id for source_id in selected if source_id not in known), None)
        if unknown is not None:
            raise ManagedAdminError(f"unknown catalog source: {unknown}")
        return selected


def _required_text(value: str, label: str) -> str:
    result = value.strip()
    if not result:
        raise ManagedAdminError(f"{label} must not be empty")
    return result


def _spotify_playlist_id(response: object) -> str:
    if not isinstance(response, dict):
        raise ManagedAdminError("Spotify playlist creation returned an invalid response")
    playlist_id = response.get("id")
    if not isinstance(playlist_id, str) or not playlist_id.strip():
        raise ManagedAdminError("Spotify playlist creation did not return a destination id")
    return playlist_id.strip()
