"""Local custom playlist drafts never perform Spotify I/O."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.custom_playlist_templates import CUSTOM_DEFAULT_COVER_ID
from news_bulletin_playlist.managed_admin import (
    ManagedAdminError,
    ManagedAdminService,
    render_spotify_description,
)
from news_bulletin_playlist.managed_provisioning import (
    ProvisioningIntent,
    ProvisioningJournal,
    ProvisioningState,
)
from news_bulletin_playlist.managed_state import (
    ManagedState,
    ManagedStateStore,
    activate_template,
)
from news_bulletin_playlist.models import SourceId


class _ForbiddenProvider:
    def __call__(self, _token: str) -> None:
        raise AssertionError("Local draft mutation must not instantiate a Spotify provider")


def _service(tmp_path: Path) -> ManagedAdminService:
    return ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=_ForbiddenProvider(),
    )


def _create(service: ManagedAdminService):
    return service.create_local_template(
        display_name="My international briefs",
        description="Curated Spanish and English headlines.",
        cover_id="international_english_news",
        source_ids=(SourceId("cnn"), SourceId("un_news_en")),
        retention_hours=48,
        max_episodes=100,
        min_duration_minutes=0,
        max_duration_minutes=30,
        languages=("es-ES", "en"),
        countries=("ES",),
    )


def test_local_create_changes_v6_to_v7_without_spotify_or_existing_playlist_changes(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    old = ManagedState(
        playlists=(
            activate_template(BUILTIN_CATALOG.playlist("spain_spanish_news"), "destination-1"),
            activate_template(
                BUILTIN_CATALOG.playlist("international_english_news"), "destination-2"
            ),
            activate_template(
                BUILTIN_CATALOG.playlist("international_polish_news"), "destination-3"
            ),
            activate_template(
                BUILTIN_CATALOG.playlist("international_spanish_long_news"), "destination-4"
            ),
        )
    )
    service.store.save(old)
    draft = _create(service)
    latest = service.store.load()
    assert latest.schema_version == 7
    assert latest.playlists == old.playlists
    assert latest.templates == (draft,)
    assert str(draft.id).startswith("custom_")
    assert draft.default_source_ids == (SourceId("cnn"), SourceId("un_news_en"))
    assert draft.languages == ("es-ES", "en")
    assert service.snapshot().custom_templates == (draft,)


def test_custom_default_cover_is_validated_without_changing_existing_drafts(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    original = _create(service)
    assert original.cover_id == "international_english_news"
    second = service.create_local_template(
        display_name="Fresh custom playlist",
        description="Neutral default artwork",
        cover_id=CUSTOM_DEFAULT_COVER_ID,
        source_ids=("cnn",),
        retention_hours=48,
        max_episodes=100,
        min_duration_minutes=0,
        max_duration_minutes=30,
    )
    state = service.store.load()
    assert state.templates == (original, second)
    assert second.cover_id == CUSTOM_DEFAULT_COVER_ID
    assert original.cover_id == "international_english_news"


def test_draft_update_hide_delete_is_local_and_keeps_stable_id(tmp_path: Path) -> None:
    service = _service(tmp_path)
    draft = _create(service)
    updated = service.update_local_template(
        draft.id,
        display_name="Renamed",
        description="A shorter selection.",
        cover_id="international_spanish_news",
        source_ids=(SourceId("cnn"),),
        retention_hours=72,
        max_episodes=25,
        min_duration_minutes=2,
        max_duration_minutes=15,
        countries=(),
        languages=("es-ES",),
        hidden=True,
    )
    assert updated.id == draft.id
    assert updated.hidden is True
    assert updated.default_source_ids == (SourceId("cnn"),)
    assert updated.min_duration_seconds == 120
    assert updated.max_duration_seconds == 900
    assert service.store.load().templates == (updated,)

    service.delete_local_template(draft.id)
    assert service.store.load().templates == ()
    assert service.store.load().schema_version == 7


def test_referenced_draft_cannot_be_deleted_even_when_playlist_paused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    draft = _create(service)
    paused = replace(
        activate_template(draft.as_playlist_template(), "spotify-existing"),
        enabled=False,
    )
    state = service.store.load()
    service.store.save(replace(state, playlists=(paused,)))
    with pytest.raises(ManagedAdminError, match="referenced"):
        service.delete_local_template(draft.id)
    assert service.store.load().templates == (draft,)


def test_pending_provisioning_keeps_draft_until_resolved(tmp_path: Path) -> None:
    service = _service(tmp_path)
    draft = _create(service)
    journal = ProvisioningJournal(tmp_path / "managed-playlist-provisioning.json")
    journal.save(
        ProvisioningIntent(
            state=ProvisioningState.REQUEST_STARTED,
            template_id=draft.id,
            display_name=draft.display_name,
            description=draft.description,
            cover_id=draft.cover_id,
            source_ids=draft.default_source_ids,
            retention_hours=draft.retention_hours,
            max_episodes=draft.max_episodes,
            min_duration_seconds=draft.min_duration_seconds,
            max_duration_seconds=draft.max_duration_seconds,
        )
    )
    with pytest.raises(ManagedAdminError, match="pending provisioning"):
        service.delete_local_template(draft.id)


def test_invalid_source_or_duration_keeps_previous_draft(tmp_path: Path) -> None:
    service = _service(tmp_path)
    draft = _create(service)
    with pytest.raises(ManagedAdminError, match="unknown catalog source"):
        service.update_local_template(
            draft.id,
            display_name="Bad",
            description="",
            cover_id=draft.cover_id,
            source_ids=("not_installed",),
            retention_hours=48,
            max_episodes=100,
            min_duration_minutes=0,
            max_duration_minutes=30,
            hidden=False,
        )
    with pytest.raises(ManagedAdminError, match="minimum duration"):
        service.update_local_template(
            draft.id,
            display_name="Bad",
            description="",
            cover_id=draft.cover_id,
            source_ids=draft.default_source_ids,
            retention_hours=48,
            max_episodes=100,
            min_duration_minutes=40,
            max_duration_minutes=30,
            hidden=False,
        )
    assert service.store.load().templates == (draft,)


class _CreateOnlySpotify:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def create_playlist(
        self, name: str, *, public: bool = True, description: str = ""
    ) -> dict[str, str]:
        assert description == render_spotify_description("Curated Spanish and English headlines.")
        self.calls.append((name, public))
        return {"id": "A" * 22}

    def change_playlist_details(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("Activation must not change Spotify metadata")

    def upload_playlist_cover(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("Cover sync must be an explicit separate operation")


def test_custom_draft_activates_through_existing_journal_without_implicit_metadata(
    tmp_path: Path,
) -> None:
    spotify = _CreateOnlySpotify()
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: spotify,
    )
    draft = _create(service)
    managed = service.activate(
        template_id=draft.id,
        display_name=draft.display_name,
        description=draft.description,
        cover_id=draft.cover_id,
        source_ids=draft.default_source_ids,
        access_token="test-token",
        min_duration_minutes=0,
        max_duration_minutes=30,
    )

    assert spotify.calls == [(draft.display_name, True)]
    assert managed.template_id == draft.id
    assert managed.destination.external_id == "A" * 22
    assert managed.source_ids == draft.default_source_ids
    assert managed.retention_hours == draft.retention_hours
    assert managed.max_episodes == draft.max_episodes
    assert managed.min_duration_seconds == draft.min_duration_seconds
    assert managed.max_duration_seconds == draft.max_duration_seconds
    assert service.store.load().templates == (draft,)
    assert service.provisioning_journal.load() is None

    with pytest.raises(ManagedAdminError, match="already managed"):
        service.activate(
            template_id=draft.id,
            display_name=draft.display_name,
            description=draft.description,
            cover_id=draft.cover_id,
            source_ids=draft.default_source_ids,
            access_token="test-token",
        )
    assert len(spotify.calls) == 1

    service.stop_managing(managed.id)
    assert service.store.load().templates == (draft,)
    assert service.store.load().playlists == ()
    assert len(spotify.calls) == 1


def test_hidden_draft_cannot_be_force_activated(tmp_path: Path) -> None:
    spotify = _CreateOnlySpotify()
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: spotify,
    )
    draft = _create(service)
    service.update_local_template(
        draft.id,
        display_name=draft.display_name,
        description=draft.description,
        cover_id=draft.cover_id,
        source_ids=draft.default_source_ids,
        retention_hours=draft.retention_hours,
        max_episodes=draft.max_episodes,
        min_duration_minutes=0,
        max_duration_minutes=30,
        hidden=True,
    )
    with pytest.raises(ManagedAdminError, match="hidden"):
        service.activate(
            template_id=draft.id,
            display_name=draft.display_name,
            description=draft.description,
            cover_id=draft.cover_id,
            source_ids=draft.default_source_ids,
            access_token="test-token",
        )
    assert spotify.calls == []
    assert service.provisioning_journal.load() is None


def test_known_custom_provisioning_recovers_without_duplicate_spotify_creation(
    tmp_path: Path,
) -> None:
    spotify = _CreateOnlySpotify()
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: spotify,
    )
    draft = _create(service)
    service.provisioning_journal.save(
        ProvisioningIntent(
            state=ProvisioningState.DESTINATION_KNOWN,
            template_id=draft.id,
            display_name=draft.display_name,
            description=draft.description,
            cover_id=draft.cover_id,
            source_ids=draft.default_source_ids,
            retention_hours=draft.retention_hours,
            max_episodes=draft.max_episodes,
            min_duration_seconds=draft.min_duration_seconds,
            max_duration_seconds=draft.max_duration_seconds,
            destination_id="B" * 22,
        )
    )
    managed = service.finalize_known_provisioning()
    assert managed is not None
    assert managed.template_id == draft.id
    assert managed.destination.external_id == "B" * 22
    assert spotify.calls == []
    assert service.provisioning_journal.load() is None
