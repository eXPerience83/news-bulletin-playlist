"""Application service for Web-managed playlist instances."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.managed_duration import (
    MAX_NEW_MANAGED_DURATION_SECONDS,
    duration_seconds_from_minutes,
    validate_duration_range,
    validate_duration_update,
    validate_min_duration_update,
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
    effective_sources,
    template_suspended_by_quarantine,
)
from news_bulletin_playlist.models import CountryCode, LanguageTag, PlaylistId, SourceId
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)
from news_bulletin_playlist.spotify.identifiers import is_spotify_id
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitSuppressed

MAX_PLAYLIST_NAME_LENGTH = 100
SPOTIFY_PLAYLIST_DESCRIPTION_LIMIT = 300
PROJECT_PROFILE_URL = "https://github.com/eXPerience83"
LEGACY_PROJECT_REPOSITORY_URL = "https://github.com/eXPerience83/news-bulletin-playlist"
LEGACY_PROJECT_DESCRIPTION_FOOTER = f"Proyecto: {LEGACY_PROJECT_REPOSITORY_URL}"
LEGACY_BILINGUAL_PROJECT_DESCRIPTION_FOOTER = f"Proyecto / Project: {LEGACY_PROJECT_REPOSITORY_URL}"
LEGACY_PROJECT_DESCRIPTION_SUFFIX = f" · by eXPerience83 · {PROJECT_PROFILE_URL}"
LEGACY_STANDALONE_PROJECT_DESCRIPTION_FOOTER = (
    f"News Bulletin Playlist{LEGACY_PROJECT_DESCRIPTION_SUFFIX}"
)
PROJECT_DESCRIPTION_FOOTER = (
    f"Made with News Bulletin Playlist · by eXPerience83 · {LEGACY_PROJECT_REPOSITORY_URL}"
)
PROJECT_DESCRIPTION_SUFFIX = f" · {PROJECT_DESCRIPTION_FOOTER}"
MAX_SPOTIFY_BASE_DESCRIPTION_LENGTH = SPOTIFY_PLAYLIST_DESCRIPTION_LIMIT - len(
    PROJECT_DESCRIPTION_SUFFIX
)
# Preserve the local description contract for existing state and ordinary Saves.
MAX_PLAYLIST_DESCRIPTION_LENGTH = SPOTIFY_PLAYLIST_DESCRIPTION_LIMIT - len(
    LEGACY_PROJECT_DESCRIPTION_SUFFIX
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


class SpotifyPlaylistCreationUncertainError(ManagedAdminError):
    """Prevent blind retries when Spotify creation may have succeeded remotely."""

    def __init__(self) -> None:
        super().__init__(
            "Spotify playlist creation outcome is unknown because Spotify did not confirm the "
            "result; inspect Spotify for a newly created playlist before retrying"
        )


class SpotifyAdminCooldownError(ManagedAdminError):
    """Report a Web API operation suppressed locally before transport."""

    def __init__(self) -> None:
        super().__init__("Spotify Web API cooldown is active; no provider request was sent")


class PlaylistProvisioningRecoveryRequired(ManagedAdminError):
    """A previous create request may have succeeded and must be recovered first."""

    def __init__(self) -> None:
        super().__init__(
            "A previous Spotify playlist creation may have succeeded; recover or clear the "
            "pending activation before creating another playlist"
        )


class SpotifyMetadataSyncOutcome(StrEnum):
    """Sanitized attribution result for explicit Spotify metadata synchronization."""

    APPLIED_WITH_ATTRIBUTION = "applied_with_attribution"


@dataclass(frozen=True, slots=True)
class SpotifyPlaylistSyncResult:
    """Successful explicit Spotify metadata/cover synchronization result."""

    playlist: ManagedPlaylist
    metadata_outcome: SpotifyMetadataSyncOutcome


class SpotifyPlaylistSyncError(ManagedAdminError):
    """Report safe per-operation results from an explicit Spotify metadata/cover sync."""

    def __init__(
        self,
        *,
        metadata_error: str | None,
        cover_error: str | None,
        metadata_outcome: SpotifyMetadataSyncOutcome | None = None,
    ) -> None:
        self.metadata_error = metadata_error
        self.cover_error = cover_error
        self.metadata_outcome = metadata_outcome
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
    custom_templates: tuple[LocalPlaylistTemplate, ...] = ()


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
            custom_templates=state.templates,
        )

    def create_local_template(
        self,
        *,
        display_name: str,
        description: str,
        cover_id: str,
        source_ids: Sequence[SourceId | str],
        retention_hours: int,
        max_episodes: int,
        min_duration_minutes: int,
        max_duration_minutes: int,
        countries: Sequence[str] = (),
        languages: Sequence[str] = (),
    ) -> LocalPlaylistTemplate:
        """Save a durable local draft; no Spotify authentication or network call."""
        state = self.store.load()
        selected = self._source_ids(source_ids, state=state, allow_empty=False)
        lower, upper = _duration_range_candidate(
            None,
            None,
            min_duration_minutes,
            max_duration_minutes,
            current_min=0,
            current_max=1800,
            activating=True,
        )
        existing = {template.id for template in state.templates}
        existing.update(template.id for template in self.catalog.playlists)
        while True:
            generated = PlaylistId(f"custom_{secrets.token_hex(8)}")
            if generated not in existing:
                break
        draft = LocalPlaylistTemplate(
            id=generated,
            display_name=_playlist_name(display_name),
            description=_playlist_description(description),
            cover_id=self._cover_id(cover_id),
            default_source_ids=selected,
            retention_hours=retention_hours,
            max_episodes=max_episodes,
            min_duration_seconds=lower,
            max_duration_seconds=upper,
            countries=tuple(CountryCode(value.strip()) for value in countries),
            languages=tuple(LanguageTag(value.strip()) for value in languages),
        )
        self._save_validated(
            replace(
                state,
                schema_version=7,
                templates=state.templates + (draft,),
            )
        )
        return draft

    def update_local_template(
        self,
        template_id: PlaylistId | str,
        *,
        display_name: str,
        description: str,
        cover_id: str,
        source_ids: Sequence[SourceId | str],
        retention_hours: int,
        max_episodes: int,
        min_duration_minutes: int,
        max_duration_minutes: int,
        hidden: bool,
        countries: Sequence[str] = (),
        languages: Sequence[str] = (),
    ) -> LocalPlaylistTemplate:
        """Edit the draft's defaults only; active Spotify instances keep snapshots."""
        state = self.store.load()
        current = self._local_template(state, template_id)
        selected = self._source_ids(
            source_ids, state=state, allow_empty=True, existing=current.default_source_ids
        )
        lower, upper = _duration_range_candidate(
            None,
            None,
            min_duration_minutes,
            max_duration_minutes,
            current_min=current.min_duration_seconds,
            current_max=current.max_duration_seconds,
            activating=False,
        )
        updated = replace(
            current,
            display_name=_playlist_name(display_name),
            description=_playlist_description(description),
            cover_id=self._cover_id(cover_id),
            default_source_ids=selected,
            retention_hours=retention_hours,
            max_episodes=max_episodes,
            min_duration_seconds=lower,
            max_duration_seconds=upper,
            countries=tuple(CountryCode(value.strip()) for value in countries),
            languages=tuple(LanguageTag(value.strip()) for value in languages),
            hidden=hidden,
        )
        self._save_validated(
            replace(
                state,
                templates=tuple(
                    updated if draft.id == current.id else draft for draft in state.templates
                ),
            )
        )
        return updated

    def delete_local_template(self, template_id: PlaylistId | str) -> None:
        """Only unreferenced drafts can be deleted; never deletes from Spotify."""
        state = self.store.load()
        current = self._local_template(state, template_id)
        if any(playlist.template_id == current.id for playlist in state.playlists):
            raise ManagedAdminError("Custom template is referenced by a managed playlist")
        intent = self.provisioning_journal.load()
        if intent is not None and intent.template_id == current.id:
            raise ManagedAdminError("Custom template is referenced by pending provisioning")
        self._save_validated(
            replace(
                state,
                templates=tuple(item for item in state.templates if item.id != current.id),
            )
        )

    def discard_quarantined_template(
        self,
        index: int,
        fingerprint: str,
        *,
        backup_confirmed: bool,
    ) -> None:
        """Explicitly discard one unreferenced corrupt draft; never touch Spotify."""
        if not backup_confirmed:
            raise ManagedAdminError("Confirm a verified backup before discarding the draft")
        state = self.store.load()
        if type(index) is not int or not 0 <= index < len(state.quarantined_templates):
            raise ManagedAdminError("quarantine entry no longer exists")
        selected = state.quarantined_templates[index]
        digest = hashlib.sha256(selected.raw_json.encode("utf-8")).hexdigest()
        if not isinstance(fingerprint, str) or not hmac.compare_digest(fingerprint, digest):
            raise ManagedAdminError("quarantine entry changed; reload Admin before proceeding")
        if any(
            template_suspended_by_quarantine(self.catalog, state, playlist.template_id)
            for playlist in state.playlists
        ):
            raise ManagedAdminError(
                "Resolve the published custom playlist before discarding quarantined data"
            )
        if self.provisioning_journal.load() is not None:
            raise ManagedAdminError("Resolve pending Spotify provisioning before repair")
        # Existing live destinations never depend on a quarantined entry here.
        # A fresh save retains every remaining record verbatim as JSON values.
        self._save_validated(
            replace(
                state,
                quarantined_templates=tuple(
                    item
                    for position, item in enumerate(state.quarantined_templates)
                    if position != index
                ),
            )
        )

    def _local_template(
        self, state: ManagedState, template_id: PlaylistId | str
    ) -> LocalPlaylistTemplate:
        match = next((item for item in state.templates if item.id == template_id), None)
        if match is None:
            raise ManagedAdminError("unknown installation-owned playlist template")
        return match

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
        min_duration_seconds: int | None = None,
        min_duration_minutes: int | None = None,
        max_duration_minutes: int | None = None,
    ) -> ManagedPlaylist:
        if self.provisioning_journal.load() is not None:
            raise PlaylistProvisioningRecoveryRequired()
        state = self.store.load()
        template = self._template(template_id, state=state)
        local = next((item for item in state.templates if item.id == template_id), None)
        if local is not None and local.hidden:
            raise ManagedAdminError("hidden custom template cannot be activated")
        if any(playlist.template_id == template.id for playlist in state.playlists):
            raise ManagedAdminError(f"playlist template {template.id} is already managed")
        duration_min, duration_max = _duration_range_candidate(
            min_duration_seconds,
            max_duration_seconds,
            min_duration_minutes,
            max_duration_minutes,
            current_min=template.duration_policy.default_min_seconds,
            current_max=template.duration_policy.default_max_seconds,
            activating=True,
        )
        intent = ProvisioningIntent(
            state=ProvisioningState.REQUEST_STARTED,
            template_id=template.id,
            display_name=_playlist_name(display_name),
            description=_playlist_description(description),
            cover_id=self._cover_id(cover_id),
            source_ids=self._source_ids(source_ids, state=state, allow_empty=False),
            retention_hours=template.retention_hours,
            max_episodes=template.max_episodes,
            max_duration_seconds=duration_max,
            min_duration_seconds=duration_min,
        )
        self.provisioning_journal.save(intent)

        client = self.client_factory(access_token)
        try:
            # Provision only the durable destination identity. Metadata and cover are explicit
            # follow-up operations so their provider behavior can never block activation.
            response = client.create_playlist(intent.display_name, public=True)
        except SpotifyRateLimitSuppressed as exc:
            self.provisioning_journal.clear()
            raise SpotifyAdminCooldownError() from exc
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
        if template_suspended_by_quarantine(self.catalog, state, intent.template_id):
            # Never fabricate an activated destination from an unresolved local draft.
            # Keep the journal for explicit operator recovery.
            return None
        template = self._template(intent.template_id, state=state)
        managed = replace(
            activate_template(template, intent.destination_id),
            display_name=intent.display_name,
            description=intent.description,
            cover_id=intent.cover_id,
            source_ids=self._source_ids(intent.source_ids, state=state, allow_empty=False),
            retention_hours=intent.retention_hours,
            max_episodes=intent.max_episodes,
            max_duration_seconds=intent.max_duration_seconds,
            min_duration_seconds=intent.min_duration_seconds,
        )
        self._save_validated(replace(state, playlists=state.playlists + (managed,)))
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
        try:
            user_id = _spotify_user_id(client.current_user())
        except SpotifyApiError as exc:
            if exc.status in {401, 403}:
                raise ManagedAdminError(
                    "Spotify ownership verification requires reconnecting Spotify to grant "
                    "the user-read-private permission"
                ) from exc
            raise
        owner_id = _spotify_playlist_owner_id(
            client.playlist_details(candidate),
            expected_playlist_id=candidate,
            expected_name=intent.display_name,
        )
        if user_id != owner_id:
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
        min_duration_seconds: int | None = None,
        min_duration_minutes: int | None = None,
        max_duration_minutes: int | None = None,
    ) -> ManagedPlaylist:
        # Saving managed configuration is deliberately local-only. Name/description are desired
        # metadata and are pushed to Spotify only by the explicit metadata & cover sync action.
        del access_token
        state = self.store.load()
        current = self._managed(state, playlist_id)
        selected_sources = self._source_ids(
            source_ids,
            state=state,
            allow_empty=not enabled,
            existing=current.source_ids,
        )
        duration_min, duration_max = _duration_range_candidate(
            min_duration_seconds,
            max_duration_seconds,
            min_duration_minutes,
            max_duration_minutes,
            current_min=current.min_duration_seconds,
            current_max=current.max_duration_seconds,
            activating=False,
        )
        updated = replace(
            current,
            enabled=enabled,
            display_name=_playlist_name(display_name),
            description=_playlist_description(description),
            cover_id=self._cover_id(cover_id),
            source_ids=selected_sources,
            max_duration_seconds=duration_max,
            min_duration_seconds=duration_min,
        )
        next_state = self._state_with_replacement(state, updated)
        self._save_validated(next_state)
        return updated

    def sync_spotify_metadata_and_cover(
        self,
        playlist_id: PlaylistId | str,
        *,
        access_token: str,
    ) -> SpotifyPlaylistSyncResult:
        state = self.store.load()
        current = self._managed(state, playlist_id)
        client = self.client_factory(access_token)
        metadata_error: str | None = None
        cover_error: str | None = None
        metadata_outcome: SpotifyMetadataSyncOutcome | None = None

        try:
            metadata_outcome = self._apply_metadata_with_attribution(client, current)
        except (SpotifyApiError, SpotifyTransportError) as exc:
            metadata_error = _safe_spotify_operation_error(exc)
        except ManagedAdminError as exc:
            metadata_error = str(exc)

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
                metadata_outcome=metadata_outcome,
            )
        if metadata_outcome is None:
            raise RuntimeError("metadata synchronization completed without an outcome")
        return SpotifyPlaylistSyncResult(
            playlist=current,
            metadata_outcome=metadata_outcome,
        )

    def set_enabled(self, playlist_id: PlaylistId | str, enabled: bool) -> ManagedPlaylist:
        state = self.store.load()
        current = self._managed(state, playlist_id)
        if enabled and not current.source_ids:
            raise ManagedAdminError("a resumed playlist must select at least one enabled source")
        if enabled:
            self._require_enabled_source(current.source_ids, state=state)
        updated = replace(current, enabled=enabled)
        self._replace(state, updated)
        return updated

    def stop_managing(self, playlist_id: PlaylistId | str) -> str:
        state = self.store.load()
        current = self._managed(state, playlist_id)
        remaining = tuple(playlist for playlist in state.playlists if playlist.id != current.id)
        self._save_validated(replace(state, playlists=remaining))
        return current.destination.external_id

    def _apply_metadata_with_attribution(
        self,
        client: PlaylistProvisioningClient,
        playlist: ManagedPlaylist,
    ) -> SpotifyMetadataSyncOutcome:
        client.change_playlist_details(
            playlist.destination.external_id,
            name=playlist.display_name,
            description=render_spotify_description(playlist.description),
        )
        return SpotifyMetadataSyncOutcome.APPLIED_WITH_ATTRIBUTION

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
        return replace(state, playlists=playlists)

    def _validate_state(self, state: ManagedState) -> None:
        compile_engine_config(self.catalog, state)

    def _save_validated(self, state: ManagedState) -> None:
        # Cross-check current source/playlist references before replacing durable state.
        self._validate_state(state)
        self.store.save(state)

    def _template(
        self, template_id: PlaylistId | str, *, state: ManagedState | None = None
    ) -> PlaylistTemplate:
        """Resolve both image-owned and installation-owned templates safely."""
        effective_state = self.store.load() if state is None else state
        local = next((item for item in effective_state.templates if item.id == template_id), None)
        if local is not None:
            return local.as_playlist_template()
        try:
            return self.catalog.playlist(template_id)
        except KeyError as exc:
            raise ManagedAdminError(str(exc)) from exc

    def _managed(self, state: ManagedState, playlist_id: PlaylistId | str) -> ManagedPlaylist:
        for playlist in state.playlists:
            if playlist.id == playlist_id:
                if template_suspended_by_quarantine(self.catalog, state, playlist.template_id):
                    raise ManagedAdminError(
                        "managed playlist has a quarantined template; repair it before editing"
                    )
                return playlist
        raise ManagedAdminError(f"unknown managed playlist: {playlist_id}")

    def _source_ids(
        self,
        source_ids: Sequence[SourceId | str],
        *,
        state: ManagedState,
        allow_empty: bool,
        existing: tuple[SourceId, ...] = (),
    ) -> tuple[SourceId, ...]:
        selected = tuple(SourceId(str(source_id).strip()) for source_id in source_ids)
        if not selected and not allow_empty:
            raise ManagedAdminError("at least one source must be selected")
        if any(not str(source_id).strip() for source_id in selected):
            raise ManagedAdminError("source id must not be empty")
        if len(selected) != len(set(selected)):
            raise ManagedAdminError("source selection contains duplicates")
        sources = effective_sources(self.catalog, state)
        known = {source.id for source in sources}
        unknown = next((source_id for source_id in selected if source_id not in known), None)
        if unknown is not None:
            raise ManagedAdminError(f"unknown catalog source: {unknown}")
        disabled = next(
            (
                source.id
                for source in sources
                if source.id in selected and not source.enabled and source.id not in existing
            ),
            None,
        )
        if disabled is not None:
            raise ManagedAdminError(f"disabled source cannot be newly assigned: {disabled}")
        if not allow_empty:
            self._require_enabled_source(selected, state=state)
        return selected

    def _require_enabled_source(
        self, selected: tuple[SourceId, ...], *, state: ManagedState
    ) -> None:
        if not any(
            source.enabled and source.id in selected
            for source in effective_sources(self.catalog, state)
        ):
            raise ManagedAdminError(
                "an active playlist must select at least one enabled source; "
                "add an enabled source or keep the playlist paused"
            )

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
    if isinstance(error, SpotifyRateLimitSuppressed):
        return "cooldown active"
    return "network error"


def render_spotify_description(base_description: str) -> str:
    """Append public generator attribution while keeping managed state footer-free."""
    base = _playlist_description(base_description)
    # Sanitize remote line breaks without changing the editable local description.
    base = base.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    if len(base) > MAX_SPOTIFY_BASE_DESCRIPTION_LENGTH:
        raise ManagedAdminError(
            "Spotify metadata was not sent: shorten the base description to "
            f"{MAX_SPOTIFY_BASE_DESCRIPTION_LENGTH} characters or fewer to fit the "
            f"{SPOTIFY_PLAYLIST_DESCRIPTION_LIMIT}-character limit with project attribution; "
            "the local description is preserved"
        )
    if not base:
        return PROJECT_DESCRIPTION_FOOTER
    return f"{base}{PROJECT_DESCRIPTION_SUFFIX}"


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
    # rendered for Spotify so repeated edits/syncs cannot duplicate the generated footer.
    result = _strip_terminal_project_footer(value)
    if len(result) > MAX_PLAYLIST_DESCRIPTION_LENGTH:
        raise ManagedAdminError(
            f"playlist description must be at most {MAX_PLAYLIST_DESCRIPTION_LENGTH} characters"
        )
    return result


def _duration_range_candidate(
    minimum: int | None,
    maximum: int | None,
    min_minutes: int | None,
    max_minutes: int | None,
    *,
    current_min: int,
    current_max: int,
    activating: bool,
) -> tuple[int, int]:
    try:
        if min_minutes is not None:
            if minimum is not None:
                raise ValueError("minimum duration supplied twice")
            minimum = duration_seconds_from_minutes(min_minutes, current_seconds=current_min)
        if max_minutes is not None:
            if maximum is not None:
                raise ValueError("maximum duration supplied twice")
            maximum = duration_seconds_from_minutes(max_minutes, current_seconds=current_max)
        candidate_min = (
            current_min
            if minimum is None
            else validate_min_duration_update(
                minimum,
                current_seconds=None if activating else current_min,
            )
        )
        candidate_max = (
            current_max
            if maximum is None
            else (
                validate_new_duration_seconds(maximum)
                if activating
                else validate_duration_update(maximum, current_seconds=current_max)
            )
        )
        validate_duration_range(candidate_min, candidate_max)
        return candidate_min, candidate_max
    except ValueError as exc:
        raise ManagedAdminError(str(exc)) from exc


def _strip_terminal_project_footer(value: str) -> str:
    result = value
    footers = (
        PROJECT_DESCRIPTION_FOOTER,
        LEGACY_STANDALONE_PROJECT_DESCRIPTION_FOOTER,
        LEGACY_BILINGUAL_PROJECT_DESCRIPTION_FOOTER,
        LEGACY_PROJECT_DESCRIPTION_FOOTER,
    )
    suffixes = (PROJECT_DESCRIPTION_SUFFIX, LEGACY_PROJECT_DESCRIPTION_SUFFIX)
    while True:
        candidate = result.rstrip()
        if candidate in footers:
            result = ""
            continue
        newline_footer = next(
            (
                footer
                for footer in footers
                if candidate.endswith(footer) and candidate[: -len(footer)].endswith(("\n", "\r"))
            ),
            None,
        )
        if newline_footer is not None:
            result = candidate[: -len(newline_footer)].rstrip()
            continue
        suffix = next((suffix for suffix in suffixes if candidate.endswith(suffix)), None)
        if suffix is not None:
            result = candidate[: -len(suffix)]
            continue
        return result


def _spotify_playlist_id(response: object) -> str:
    if not isinstance(response, dict):
        raise ManagedAdminError("Spotify playlist creation returned an invalid response")
    playlist_id = response.get("id")
    if not isinstance(playlist_id, str) or not playlist_id.strip():
        raise ManagedAdminError("Spotify playlist creation did not return a destination id")
    return playlist_id.strip()


def _spotify_playlist_id_text(value: str) -> str:
    candidate = value.strip()
    if not is_spotify_id(candidate):
        raise ManagedAdminError("Spotify playlist id must be a 22-character base62 id")
    return candidate


def _spotify_user_id(response: object) -> str:
    if not isinstance(response, dict):
        raise ManagedAdminError("Spotify user verification returned an invalid response")
    value = response.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ManagedAdminError("Spotify user verification did not return an owner id")
    return value.strip()


def _spotify_playlist_owner_id(
    response: object,
    *,
    expected_playlist_id: str,
    expected_name: str,
) -> str:
    if not isinstance(response, dict) or response.get("id") != expected_playlist_id:
        raise ManagedAdminError("Spotify playlist verification returned an unexpected destination")
    owner = response.get("owner")
    if not isinstance(owner, dict):
        raise ManagedAdminError("Spotify playlist verification did not return an owner")
    value = owner.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ManagedAdminError("Spotify playlist verification did not return an owner")
    if response.get("name") != expected_name:
        raise ManagedAdminError("Spotify playlist name does not match the pending activation")
    return value.strip()
