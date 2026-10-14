"""Local custom playlist drafts never perform Spotify I/O."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_admin import ManagedAdminError, ManagedAdminService
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
