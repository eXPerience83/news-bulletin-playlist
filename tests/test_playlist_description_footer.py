from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_admin import (
    MAX_PLAYLIST_DESCRIPTION_LENGTH,
    PROJECT_DESCRIPTION_FOOTER,
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


def test_renderer_appends_project_footer_exactly_once() -> None:
    rendered = render_spotify_description("Descripción")

    assert rendered == f"Descripción\n\n{PROJECT_DESCRIPTION_FOOTER}"
    assert rendered.count(PROJECT_DESCRIPTION_FOOTER) == 1


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
    assert spotify.create_calls == [
        (template.display_name, render_spotify_description("Base editable"))
    ]


def test_repeated_metadata_edits_do_not_duplicate_footer(tmp_path: Path) -> None:
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
    assert descriptions == [
        render_spotify_description("Segunda"),
        render_spotify_description("Segunda"),
    ]
    assert all(value.count(PROJECT_DESCRIPTION_FOOTER) == 1 for value in descriptions)


def test_description_limit_reserves_space_for_project_footer(tmp_path: Path) -> None:
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
    with pytest.raises(ManagedAdminError, match="project link fits"):
        other_service.activate(
            template_id=template.id,
            display_name=template.display_name,
            description=maximum + "x",
            cover_id=template.cover_id,
            source_ids=template.default_source_ids,
            access_token="token",
        )
