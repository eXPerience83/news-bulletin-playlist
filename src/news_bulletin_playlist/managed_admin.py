"""Application service for Web-managed playlist instances."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.managed_duration import (
    MAX_NEW_MANAGED_DURATION_SECONDS,
    validate_duration_update,
    validate_new_duration_seconds,
)
from news_bulletin_playlist.managed_provisioning import (
    MANAGED_PROVISIONING_FILENAME,
    ProvisioningIntent,
    ProvisioningJournal,
    ProvisioningJournalError,
    ProvisioningState,
)
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
LEGACY_PROJECT_DESCRIPTION_FOOTER = f"Proyecto: {PROJECT_REPOSITORY_URL}"
PROJECT_DESCRIPTION_FOOTER = f"Proyecto / Project: {PROJECT_REPOSITORY_URL}"
PROJECT_DESCRIPTION_SEPARATOR = "\n\n"
MAX_PLAYLIST_DESCRIPTION_LENGTH = (
    SPOTIFY_PLAYLIST_DESCRIPTION_LIMIT
    - len(PROJECT_DESCRIPTION_SEPARATOR)
    - len(PROJECT_DESCRIPTION_FOOTER)
)
MAX_PLAYLIST_DURATION_MINUTES = MAX_NEW_MANAGED_DURATION_SECONDS // 60


class ManagedAdminError(RuntimeError):
    """Safe operator-facing error for managed playlist administration."""


class SpotifyPlaylistProvisioningError(ManagedAdminError):
    """Report a created Spotify destination when local persistence did not complete."""

    def __init__(self, playlist_id: str) -> None:
        super().__init__(
            "Spotify playlist was created but local managed state could not be saved; "
            f"destination id {playlist_id!r} now requires operator reconciliation"
        )
        self.playlist_id = playlist_id


class SpotifyPlaylistPersistenceError(ManagedAdminError):
    """Compatibility error retained for callers from pre-local-only metadata updates."""

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


class PlaylistProvisioningRecoveryRequired(ManagedAdminError):
    """A previous create request may have succeeded and must be recovered first."""

    def __init__(self) -> None:
        super().__init__(
            "A previous Spotify playlist creation may have succeeded; recover or clear the "
            "pending activation before creating another playlist"
        )


class SpotifyPlaylistSyncError(ManagedAdminError):
    """Report safe per-operation results from an explicit Spotify metadata/cover sync."""

    def __init__(
        self,
        *,
        metadata_error: str | None,
        cover_error: str | None,
    ) -> None:
        self.metadata_error = metadata_error
        self.cover_error = cover_error
        metadata = "applied" if metadata_error is None else f"failed ({metadata_error})"
        cover = "applied" if cover_error is None else f"failed ({cover_error})"
        super().__init__(f"Spotify explicit sync result: metadata {metadata}; cover {cover}")


class PlaylistProvisioningClient(Protocol):
    def create_playlist(
        self, name: str, *, public: bool = True, description: str = ""
    ) -> dict[str, Any]: ...

    def current_user(self) -> dict[str, Any]: ...

    def playlist_details(self, playlist_id: str) -> dict[str, Any]: ...

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
    provisioning_intent: ProvisioningIntent | None = None


class ManagedAdminService:
    """Mutate installation-owned choices while keeping the built-in catalog immutable."""

    def __init__(
        self,
        store: ManagedStateStore,
        *,
        catalog: BuiltInCatalog = BUILTIN_CATALOG,
        client_factory: PlaylistClientFactory | None = None,
        cover_loader: CoverAssetLoader | None = None,
        provisioning_journal: ProvisioningJournal | None = None,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.client_factory = client_factory or SpotifyClient
        self.cover_loader = cover_loader
        self.provisioning_journal = provisioning_journal or ProvisioningJournal(
            store.path.with_name(MANAGED_PROVISIONING_FILENAME)
        )

    def snapshot(self) -> ManagedAdminSnapshot:
        self.finalize_known_provisioning()
        state = self.store.load()
        managed_templates = {playlist.template_id for playlist in state.playlists}
        available = tuple(
            template for template in self.catalog.playlists if template.id not in managed_templates
        )
        return ManagedAdminSnapshot(
            managed=state.playlists,
            available_templates=available,
            provisioning_intent=self.provisioning_journal.load(),
        )

    def activate(
        self,
        *,
        template_id: PlaylistId | str,
        display_name: str,
        description: str,
        cover_id: str,
        source_ids: Sequence[SourceId | str],
        access_token: str,
        max_duration_seconds: int | None = None,
    ) -> ManagedPlaylist:
        if self.provisioning_journal.load() is not None:
            raise PlaylistProvisioningRecoveryRequired()
        state = self.store.load()
        template = self._template(template_id)
        if any(playlist.template_id == template.id for playlist in state.playlists):
            raise ManagedAdminError(f"playlist template {template.id} is already managed")
        intent = ProvisioningIntent(
            state=ProvisioningState.REQUEST_STARTED,
            template_id=template.id,
            display_name=_playlist_name(display_name),
            description=_playlist_description(description),
            cover_id=self._cover_id(cover_id),
            source_ids=self._source_ids(source_ids, allow_empty=False),
            max_duration_seconds=(
                template.duration_policy.default_max_seconds
                if max_duration_seconds is None
                else _new_duration_max_seconds(max_duration_seconds)
            ),
        )
        self.provisioning_journal.save(intent)

        client = self.client_factory(access_token)
        try:
            # Provision only the durable destination identity. Metadata and cover are explicit
            # follow-up operations so their provider behavior can never block activation.
            response = client.create_playlist(intent.display_name, public=True)
        except SpotifyApiError as exc:
            if exc.status < 500:
                self.provisioning_journal.clear()
                raise
            raise SpotifyPlaylistCreationUncertainError() from exc
        except SpotifyTransportError as exc:
            raise SpotifyPlaylistCreationUncertainError() from exc
        try:
            destination_id = _spotify_playlist_id(response)
        except ManagedAdminError as exc:
            raise SpotifyPlaylistCreationUncertainError() from exc
        try:
            self.provisioning_journal.save(intent.with_destination(destination_id))
        except ProvisioningJournalError as exc:
            raise SpotifyPlaylistCreationUncertainError() from exc
        try:
            managed = self.finalize_known_provisioning()
            assert managed is not None
            return managed
        except (ManagedStateError, OSError, ProvisioningJournalError) as exc:
            raise SpotifyPlaylistProvisioningError(destination_id) from exc

    def finalize_known_provisioning(self) -> ManagedPlaylist | None:
        intent = self.provisioning_journal.load()
        if intent is None or intent.state is ProvisioningState.REQUEST_STARTED:
            return None
        assert intent.destination_id is not None
        state = self.store.load()
        existing = next(
            (
                playlist
                for playlist in state.playlists
                if playlist.template_id == intent.template_id
            ),
            None,
        )
        if existing is not None:
            if existing.destination.external_id != intent.destination_id:
                raise PlaylistProvisioningRecoveryRequired()
            self.provisioning_journal.clear()
            return existing
        template = self._template(intent.template_id)
        managed = replace(
            activate_template(template, intent.destination_id),
            display_name=intent.display_name,
            description=intent.description,
            cover_id=intent.cover_id,
            source_ids=self._source_ids(intent.source_ids, allow_empty=False),
            max_duration_seconds=intent.max_duration_seconds,
        )
        self._save_validated(
            ManagedState(
                schema_version=state.schema_version,
                playlists=state.playlists + (managed,),
            )
        )
        self.provisioning_journal.clear()
        return managed

    def adopt_uncertain_provisioning(
        self,
        destination_id: str,
        *,
        access_token: str,
    ) -> ManagedPlaylist:
        intent = self.provisioning_journal.load()
        if intent is None or intent.state is not ProvisioningState.REQUEST_STARTED:
            raise ManagedAdminError("there is no uncertain playlist activation to adopt")
        candidate = _spotify_playlist_id_text(destination_id)
        client = self.client_factory(access_token)
        if _spotify_user_id(client.current_user()) != _spotify_playlist_owner_id(
            client.playlist_details(candidate), expected_playlist_id=candidate
        ):
            raise ManagedAdminError("Spotify playlist is not owned by the authorized user")
        self.provisioning_journal.save(intent.with_destination(candidate))
        result = self.finalize_known_provisioning()
        assert result is not None
        return result

    def clear_uncertain_provisioning(self) -> None:
        intent = self.provisioning_journal.load()
        if intent is None or intent.state is not ProvisioningState.REQUEST_STARTED:
            raise ManagedAdminError("only an uncertain playlist activation can be cleared")
        self.provisioning_journal.clear()

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
        max_duration_seconds: int | None = None,
    ) -> ManagedPlaylist:
        # Saving managed configuration is deliberately local-only. Name/description are desired
        # metadata and are pushed to Spotify only by the explicit metadata & cover sync action.
        del access_token
        state = self.store.load()
        current = self._managed(state, playlist_id)
        selected_sources = self._source_ids(source_ids, allow_empty=not enabled)
        duration_max = (
            current.max_duration_seconds
            if max_duration_seconds is None
            else _updated_duration_max_seconds(
                max_duration_seconds,
                current_seconds=current.max_duration_seconds,
            )
        )
        updated = replace(
            current,
            enabled=enabled,
            display_name=_playlist_name(display_name),
            description=_playlist_description(description),
            cover_id=self._cover_id(cover_id),
            source_ids=selected_sources,
            max_duration_seconds=duration_max,
        )
        next_state = self._state_with_replacement(state, updated)
        self._save_validated(next_state)
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
        metadata_error: str | None = None
        cover_error: str | None = None

        try:
            self._apply_metadata_with_attribution_fallback(client, current)
        except (SpotifyApiError, SpotifyTransportError) as exc:
            metadata_error = _safe_spotify_operation_error(exc)

        try:
            self._upload_cover_explicit(
                client,
                current.destination.external_id,
                current.cover_id,
            )
        except (SpotifyApiError, SpotifyTransportError) as exc:
            cover_error = _safe_spotify_operation_error(exc)
        except OSError, ValueError:
            cover_error = "local cover error"

        if metadata_error is not None or cover_error is not None:
            raise SpotifyPlaylistSyncError(
                metadata_error=metadata_error,
                cover_error=cover_error,
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
        self._save_validated(ManagedState(schema_version=state.schema_version, playlists=remaining))
        return current.destination.external_id

    def _apply_metadata_with_attribution_fallback(
        self,
        client: PlaylistProvisioningClient,
        playlist: ManagedPlaylist,
    ) -> None:
        try:
            client.change_playlist_details(
                playlist.destination.external_id,
                name=playlist.display_name,
                description=render_spotify_description(playlist.description),
            )
        except SpotifyApiError as exc:
            if exc.status != 400:
                raise
            # Attribution is optional. Retry only a definitive validation failure; auth,
            # rate-limit, server and transport failures must remain visible to the operator.
            client.change_playlist_details(
                playlist.destination.external_id,
                name=playlist.display_name,
                description=_playlist_description(playlist.description),
            )

    def _upload_cover_explicit(
        self,
        client: PlaylistProvisioningClient,
        playlist_id: str,
        cover_id: str,
    ) -> None:
        if self.cover_loader is None:
            raise ValueError("bundled cover loader is unavailable")
        upload_playlist_cover = getattr(client, "upload_playlist_cover", None)
        if not callable(upload_playlist_cover):
            raise ValueError("Spotify client does not support cover upload")
        jpeg_bytes = self.cover_loader(cover_id)
        upload_playlist_cover(playlist_id, jpeg_bytes)

    def _replace(self, state: ManagedState, updated: ManagedPlaylist) -> None:
        self._save_validated(self._state_with_replacement(state, updated))

    def _state_with_replacement(
        self,
        state: ManagedState,
        updated: ManagedPlaylist,
    ) -> ManagedState:
        playlists = tuple(
            updated if playlist.id == updated.id else playlist for playlist in state.playlists
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


def _safe_spotify_operation_error(
    error: SpotifyApiError | SpotifyTransportError,
) -> str:
    if isinstance(error, SpotifyApiError):
        return f"HTTP {error.status}"
    return "network error"


def render_spotify_description(base_description: str) -> str:
    """Append a plain-text project URL while keeping managed state footer-free."""
    base = _playlist_description(base_description)
    if not base:
        return PROJECT_DESCRIPTION_FOOTER
    return f"{base}{PROJECT_DESCRIPTION_SEPARATOR}{PROJECT_DESCRIPTION_FOOTER}"


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
    # Keep attribution out of editable managed state. It is appended only when metadata is
    # rendered for Spotify so repeated edits/syncs cannot duplicate the project URL.
    result = _strip_terminal_project_footer(value)
    if len(result) > MAX_PLAYLIST_DESCRIPTION_LENGTH:
        raise ManagedAdminError(
            f"playlist description must be at most {MAX_PLAYLIST_DESCRIPTION_LENGTH} characters"
        )
    return result


def _new_duration_max_seconds(value: int) -> int:
    try:
        return validate_new_duration_seconds(value)
    except ValueError as exc:
        raise ManagedAdminError(str(exc)) from exc


def _updated_duration_max_seconds(value: int, *, current_seconds: int) -> int:
    try:
        return validate_duration_update(value, current_seconds=current_seconds)
    except ValueError as exc:
        raise ManagedAdminError(str(exc)) from exc


def _strip_terminal_project_footer(value: str) -> str:
    result = value
    footers = (PROJECT_DESCRIPTION_FOOTER, LEGACY_PROJECT_DESCRIPTION_FOOTER)
    while True:
        candidate = result.rstrip()
        matched_footer = next(
            (footer for footer in footers if candidate == footer or candidate.endswith(footer)),
            None,
        )
        if matched_footer is None:
            return result
        if candidate == matched_footer:
            result = ""
            continue
        prefix = candidate[: -len(matched_footer)]
        if not prefix.endswith(("\n", "\r")):
            return result
        result = prefix.rstrip()


def _spotify_playlist_id(response: object) -> str:
    if not isinstance(response, dict):
        raise ManagedAdminError("Spotify playlist creation returned an invalid response")
    playlist_id = response.get("id")
    if not isinstance(playlist_id, str) or not playlist_id.strip():
        raise ManagedAdminError("Spotify playlist creation did not return a destination id")
    return playlist_id.strip()


def _spotify_playlist_id_text(value: str) -> str:
    candidate = value.strip()
    if len(candidate) != 22 or not candidate.isalnum():
        raise ManagedAdminError("Spotify playlist id must be a 22-character base62 id")
    return candidate


def _spotify_user_id(response: object) -> str:
    if not isinstance(response, dict):
        raise ManagedAdminError("Spotify user verification returned an invalid response")
    value = response.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ManagedAdminError("Spotify user verification did not return an owner id")
    return value.strip()


def _spotify_playlist_owner_id(response: object, *, expected_playlist_id: str) -> str:
    if not isinstance(response, dict) or response.get("id") != expected_playlist_id:
        raise ManagedAdminError("Spotify playlist verification returned an unexpected destination")
    owner = response.get("owner")
    if not isinstance(owner, dict):
        raise ManagedAdminError("Spotify playlist verification did not return an owner")
    value = owner.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ManagedAdminError("Spotify playlist verification did not return an owner")
    return value.strip()
