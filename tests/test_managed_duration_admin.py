from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_admin_web import (
    max_duration_seconds_from_form,
    render_managed_admin_page,
)
from news_bulletin_playlist.managed_state import ManagedStateStore, compile_engine_config
from news_bulletin_playlist.spotify.auth import AuthorizationState


class _SpotifyClient:
    def __init__(self) -> None:
        self.update_calls: list[tuple[str, str, str]] = []

    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:
        del name, description
        return {"id": "duration-destination"}

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
    def __init__(self) -> None:
        self.client = _SpotifyClient()
        self.tokens: list[str] = []

    def __call__(self, token: str) -> _SpotifyClient:
        self.tokens.append(token)
        return self.client


def test_duration_form_parses_whole_minutes_and_rejects_invalid_values() -> None:
    assert max_duration_seconds_from_form({}) is None
    assert max_duration_seconds_from_form({"max_duration_minutes": ["30"]}) == 1800

    with pytest.raises(ValueError, match="between 1"):
        max_duration_seconds_from_form({"max_duration_minutes": ["0"]})
    with pytest.raises(ValueError, match="integer"):
        max_duration_seconds_from_form({"max_duration_minutes": ["30.5"]})
    with pytest.raises(ValueError, match="Exactly one"):
        max_duration_seconds_from_form({"max_duration_minutes": ["30", "60"]})


def test_duration_only_update_persists_without_spotify_metadata_io(tmp_path: Path) -> None:
    factory = _Factory()
    store = ManagedStateStore(tmp_path / "managed-state.json")
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

    assert managed.max_duration_seconds == 1800
    updated = service.update(
        managed.id,
        display_name=managed.display_name,
        description=managed.description,
        cover_id=managed.cover_id,
        source_ids=managed.source_ids,
        enabled=True,
        access_token=None,
        max_duration_seconds=900,
    )

    assert updated.max_duration_seconds == 900
    assert factory.tokens == ["create-token"]
    assert factory.client.update_calls == []
    effective = compile_engine_config(BUILTIN_CATALOG, store.load())
    assert effective.playlists[0].duration_policy.default_max_seconds == 900


def test_admin_page_exposes_current_duration_ceiling(tmp_path: Path) -> None:
    factory = _Factory()
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=factory,
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="create-token",
        max_duration_seconds=1800,
    )

    page = render_managed_admin_page(
        snapshot=service.snapshot(),
        catalog=BUILTIN_CATALOG,
        spotify_state=AuthorizationState.CONNECTED,
        csrf_token="csrf",
        last_cycle=None,
        lan_mode=True,
    ).decode("utf-8")

    assert 'name="max_duration_minutes"' in page
    assert 'value="30"' in page
    assert str(managed.id) in page
