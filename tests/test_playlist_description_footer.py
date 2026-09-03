from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_admin import (
    MAX_PLAYLIST_DESCRIPTION_LENGTH,
    PROJECT_DESCRIPTION_FOOTER,
    PROJECT_REPOSITORY_URL,
    ManagedAdminError,
    ManagedAdminService,
    render_spotify_description,
)
from news_bulletin_playlist.managed_state import ManagedStateStore


class _Spotify:
    def __init__(self) -> None:
        self.create_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, str, str]] = []

    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:
        self.create_calls.append((name, description))
        return {"id": "destination"}

    def change_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        self.update_calls.append((playlist_id, name, description))
        return {}


def test_renderer_sends_only_editable_base_description() -> None:
    rendered = render_spotify_description("Descripción")

    assert rendered == "Descripción"
    assert PROJECT_REPOSITORY_URL not in rendered
    assert PROJECT_DESCRIPTION_FOOTER not in rendered


@pytest.mark.parametrize(
    "contaminated",
    [
        f"Descripción\n\n{PROJECT_DESCRIPTION_FOOTER}",
        f"Descripción\r\n\r\n{PROJECT_DESCRIPTION_FOOTER}\r\n",
        (
            f"Descripción\n\n{PROJECT_DESCRIPTION_FOOTER}"
            f"\n\n{PROJECT_DESCRIPTION_FOOTER}"
        ),
    ],
)
def test_renderer_strips_legacy_terminal_project_footer(contaminated: str) -> None:
    assert render_spotify_description(contaminated) == "Descripción"


def test_managed_state_keeps_only_editable_base_description(tmp_path: Path) -> None:
    spotify = _Spotify()
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: spotify,
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")

    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description="Base editable",
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    assert managed.description == "Base editable"
    assert service.snapshot().managed[0].description == "Base editable"
    assert spotify.create_calls == [(template.display_name, "Base editable")]


def test_repeated_metadata_edits_send_base_description_only(tmp_path: Path) -> None:
    spotify = _Spotify()
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: spotify,
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description="Primera",
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    first = service.update(
        managed.id,
        display_name="Noticias España 1",
        description="Segunda",
        cover_id=managed.cover_id,
        source_ids=managed.source_ids,
        enabled=True,
        access_token="token",
    )
    service.update(
        managed.id,
        display_name="Noticias España 2",
        description=first.description,
        cover_id=managed.cover_id,
        source_ids=managed.source_ids,
        enabled=True,
        access_token="token",
    )

    descriptions = [call[2] for call in spotify.update_calls]
    assert descriptions == ["Segunda", "Segunda"]
    assert all(PROJECT_REPOSITORY_URL not in value for value in descriptions)


def test_metadata_update_strips_terminal_footer_from_editable_state(
    tmp_path: Path,
) -> None:
    spotify = _Spotify()
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: spotify,
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description="Base editable",
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    updated = service.update(
        managed.id,
        display_name=managed.display_name,
        description=f"Editada\r\n\r\n{PROJECT_DESCRIPTION_FOOTER}\r\n",
        cover_id=managed.cover_id,
        source_ids=managed.source_ids,
        enabled=True,
        access_token="token",
    )

    assert updated.description == "Editada"
    assert service.snapshot().managed[0].description == "Editada"
    assert spotify.update_calls[-1][2] == "Editada"


def test_source_only_update_cleans_legacy_footer_without_spotify_metadata_write(
    tmp_path: Path,
) -> None:
    spotify = _Spotify()
    store = ManagedStateStore(tmp_path / "managed-state.json")
    service = ManagedAdminService(store, client_factory=lambda _token: spotify)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description="Base editable",
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )
    state = store.load()
    contaminated = replace(
        managed,
        description=f"Base editable\n\n{PROJECT_DESCRIPTION_FOOTER}",
    )
    store.save(replace(state, playlists=(contaminated,)))

    updated = service.update(
        managed.id,
        display_name=managed.display_name,
        description=contaminated.description,
        cover_id=managed.cover_id,
        source_ids=managed.source_ids[:-1],
        enabled=True,
        access_token=None,
    )

    assert updated.description == "Base editable"
    assert updated.source_ids == managed.source_ids[:-1]
    assert store.load().playlists[0].description == "Base editable"
    assert spotify.update_calls == []


def test_legacy_footer_is_removed_before_description_limit_validation(tmp_path: Path) -> None:
    spotify = _Spotify()
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: spotify,
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    maximum = "x" * MAX_PLAYLIST_DESCRIPTION_LENGTH

    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=f"{maximum}\n\n{PROJECT_DESCRIPTION_FOOTER}",
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    assert managed.description == maximum
    assert spotify.create_calls[-1][1] == maximum
    assert len(spotify.create_calls[-1][1]) == 300


def test_description_can_use_full_spotify_limit_but_not_exceed_it(tmp_path: Path) -> None:
    spotify = _Spotify()
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: spotify,
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    maximum = "x" * MAX_PLAYLIST_DESCRIPTION_LENGTH

    service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=maximum,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    assert len(spotify.create_calls[0][1]) == 300

    other_service = ManagedAdminService(
        ManagedStateStore(tmp_path / "other-managed-state.json"),
        client_factory=lambda _token: _Spotify(),
    )
    with pytest.raises(ManagedAdminError, match="at most 300 characters"):
        other_service.activate(
            template_id=template.id,
            display_name=template.display_name,
            description=maximum + "x",
            cover_id=template.cover_id,
            source_ids=template.default_source_ids,
            access_token="token",
        )
