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
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)

MAX_PLAYLIST_NAME_LENGTH = 100
SPOTIFY_PLAYLIST_DESCRIPTION_LIMIT = 300
PROJECT_REPOSITORY_URL = "https://github.com/eXPerience83/news-bulletin-playlist"
PROJECT_DESCRIPTION_FOOTER = f"Proyecto: {PROJECT_REPOSITORY_URL}"
_DESCRIPTION_SEPARATOR = "\n\n"
MAX_PLAYLIST_DESCRIPTION_LENGTH = (
    SPOTIFY_PLAYLIST_DESCRIPTION_LIMIT
    - len(_DESCRIPTION_SEPARATOR)
    - len(PROJECT_DESCRIPTION_FOOTER)
)


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


class SpotifyPlaylistCreationUncertainError(ManagedAdminError):
    """Prevent blind retries when Spotify creation may have succeeded remotely."""

    def __init__(self) -> None:
        super().__init__(
            "Spotify playlist creation outcome is unknown because Spotify did not confirm the "
            "result; inspect Spotify for a newly created playlist before retrying"
        )


class PlaylistProvisioningClient(Protocol):
    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]: ...

    def change_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]: ...

    def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict[str, Any]: ...


PlaylistClientFactory = Callable[[str], PlaylistProvisioningClient]
CoverAssetLoader = Callable[[str], bytes]


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
        cover_loader: CoverAssetLoader | None = None,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.client_factory = client_factory or SpotifyClient
        self.cover_loader = cover_loader

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
        name = _playlist_name(display_name)
        safe_description = _playlist_description(description)
        cover = self._cover_id(cover_id)

        client = self.client_factory(access_token)
        try:
            response = client.create_private_playlist(
                name,
                description=render_spotify_description(safe_description),
            )
        except SpotifyApiError as exc:
            if exc.status < 500:
                raise
            raise SpotifyPlaylistCreationUncertainError() from exc
        except SpotifyTransportError as exc:
            raise SpotifyPlaylistCreationUncertainError() from exc
        try:
            destination_id = _spotify_playlist_id(response)
        except ManagedAdminError as exc:
            raise SpotifyPlaylistCreationUncertainError() from exc
        managed = replace(
            activate_template(template, destination_id),
            display_name=name,
            description=safe_description,
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
        self._best_effort_cover_upload(client, destination_id, cover)
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
        access_token: str | None,
    ) -> ManagedPlaylist:
        state = self.store.load()
        current = self._managed(state, playlist_id)
        selected_sources = self._source_ids(source_ids, allow_empty=not enabled)
        updated = replace(
            current,
            enabled=enabled,
            display_name=_playlist_name(display_name),
            description=_playlist_description(description),
            cover_id=self._cover_id(cover_id),
            source_ids=selected_sources,
        )
        next_state = self._state_with_replacement(state, updated)
        self._validate_state(next_state)

        metadata_changed = (
            updated.display_name != current.display_name
            or updated.description != current.description
        )
        spotify_metadata_updated = False
        if metadata_changed:
            if access_token is None:
                raise ManagedAdminError(
                    "Spotify must be connected to change playlist name or description"
                )
            self.client_factory(access_token).change_playlist_details(
                current.destination.external_id,
                name=updated.display_name,
                description=render_spotify_description(updated.description),
            )
            spotify_metadata_updated = True
        try:
            self.store.save(next_state)
        except (ManagedStateError, OSError) as exc:
            if spotify_metadata_updated:
                raise SpotifyPlaylistPersistenceError(
                    current.destination.external_id
                ) from exc
            raise
        return updated

    def sync_spotify_metadata_and_cover(
        self,
        playlist_id: PlaylistId | str,
        *,
        access_token: str,
    ) -> ManagedPlaylist:
        state = self.store.load()
        current = self._managed(state, playlist_id)
        client = self.client_factory(access_token)
        client.change_playlist_details(
            current.destination.external_id,
            name=current.display_name,
            description=render_spotify_description(current.description),
        )
        self._best_effort_cover_upload(
            client,
            current.destination.external_id,
            current.cover_id,
        )
        return current

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

    def _best_effort_cover_upload(
        self,
        client: PlaylistProvisioningClient,
        playlist_id: str,
        cover_id: str,
    ) -> None:
        if self.cover_loader is None:
            return
        try:
            jpeg_bytes = self.cover_loader(cover_id)
            client.upload_playlist_cover(playlist_id, jpeg_bytes)
        except (OSError, ValueError, SpotifyApiError, SpotifyTransportError):
            # Cover art is product metadata. It must never block playlist state or bulletin sync.
            return

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

    def _cover_id(self, value: str) -> str:
        cover_id = _required_text(value, "cover id")
        known = {template.cover_id for template in self.catalog.playlists}
        if cover_id not in known:
            raise ManagedAdminError(f"unknown bundled cover: {cover_id}")
        return cover_id


def render_spotify_description(base_description: str) -> str:
    """Render the product-owned Spotify description without mutating managed state."""
    base = _playlist_description(base_description)
    if not base:
        return PROJECT_DESCRIPTION_FOOTER
    return f"{base}{_DESCRIPTION_SEPARATOR}{PROJECT_DESCRIPTION_FOOTER}"


def _required_text(value: str, label: str) -> str:
    result = value.strip()
    if not result:
        raise ManagedAdminError(f"{label} must not be empty")
    return result


def _playlist_name(value: str) -> str:
    result = _required_text(value, "playlist name")
    if len(result) > MAX_PLAYLIST_NAME_LENGTH:
        raise ManagedAdminError(
            f"playlist name must be at most {MAX_PLAYLIST_NAME_LENGTH} characters"
        )
    return result


def _playlist_description(value: str) -> str:
    if len(value) > MAX_PLAYLIST_DESCRIPTION_LENGTH:
        raise ManagedAdminError(
            "playlist description must be at most "
            f"{MAX_PLAYLIST_DESCRIPTION_LENGTH} characters so the project link fits"
        )
    rendered_length = len(value) + len(_DESCRIPTION_SEPARATOR) + len(PROJECT_DESCRIPTION_FOOTER)
    if value and rendered_length > SPOTIFY_PLAYLIST_DESCRIPTION_LIMIT:
        raise ManagedAdminError("rendered Spotify playlist description is too long")
    return value


def _spotify_playlist_id(response: object) -> str:
    if not isinstance(response, dict):
        raise ManagedAdminError("Spotify playlist creation returned an invalid response")
    playlist_id = response.get("id")
    if not isinstance(playlist_id, str) or not playlist_id.strip():
        raise ManagedAdminError("Spotify playlist creation did not return a destination id")
    return playlist_id.strip()
