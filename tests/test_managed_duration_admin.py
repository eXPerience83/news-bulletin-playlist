from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_admin import ManagedAdminError, ManagedAdminService
from news_bulletin_playlist.managed_admin_web import (
    max_duration_seconds_from_form,
    render_managed_admin_page,
)
from news_bulletin_playlist.managed_duration import MAX_NEW_MANAGED_DURATION_SECONDS
from news_bulletin_playlist.managed_state import (
    ManagedState,
    ManagedStateStore,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.spotify.auth import AuthorizationState


class _SpotifyClient:
    def __init__(self) -> None:
        self.update_calls: list[tuple[str, str, str]] = []

    def create_playlist(
        self, name: str, *, public: bool = True, description: str = ""
    ) -> dict[str, Any]:
        assert public is True
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


def test_duration_form_round_trips_minutes_and_seconds_and_rejects_invalid_values() -> None:
    assert max_duration_seconds_from_form({}) is None
    assert max_duration_seconds_from_form({"max_duration_minutes": ["30"]}) == 1800
    assert (
        max_duration_seconds_from_form(
            {"max_duration_minutes": ["20"], "max_duration_seconds_remainder": ["34"]}
        )
        == 1234
    )

    with pytest.raises(ValueError, match="positive"):
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


def test_legacy_seconds_render_and_round_trip_exactly(tmp_path: Path) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    store = ManagedStateStore(tmp_path / "managed-state.json")
    legacy = activate_template(template, "legacy-destination")
    store.save(ManagedState(playlists=(replace(legacy, max_duration_seconds=1234),)))
    service = ManagedAdminService(store, client_factory=_Factory())

    page = render_managed_admin_page(
        snapshot=service.snapshot(),
        catalog=BUILTIN_CATALOG,
        spotify_state=AuthorizationState.CONNECTED,
        csrf_token="csrf",
        last_cycle=None,
        lan_mode=True,
    ).decode()
    assert 'name="max_duration_minutes" required min="0"' in page
    assert 'value="20"' in page
    assert 'name="max_duration_seconds_remainder"' in page
    assert 'value="34"' in page
    parsed = max_duration_seconds_from_form(
        {"max_duration_minutes": ["20"], "max_duration_seconds_remainder": ["34"]}
    )
    current = service.snapshot().managed[0]
    updated = service.update(
        current.id,
        display_name=current.display_name,
        description=current.description,
        cover_id=current.cover_id,
        source_ids=current.source_ids,
        enabled=True,
        access_token=None,
        max_duration_seconds=parsed,
    )
    assert updated.max_duration_seconds == 1234


def test_duration_ceiling_allows_boundary_and_preserves_unchanged_legacy_value(
    tmp_path: Path,
) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    store = ManagedStateStore(tmp_path / "managed-state.json")
    service = ManagedAdminService(store, client_factory=_Factory())
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="create-token",
        max_duration_seconds=MAX_NEW_MANAGED_DURATION_SECONDS,
    )
    assert managed.max_duration_seconds == MAX_NEW_MANAGED_DURATION_SECONDS
    with pytest.raises(ManagedAdminError, match="at most"):
        service.update(
            managed.id,
            display_name=managed.display_name,
            description=managed.description,
            cover_id=managed.cover_id,
            source_ids=managed.source_ids,
            enabled=True,
            access_token=None,
            max_duration_seconds=MAX_NEW_MANAGED_DURATION_SECONDS + 1,
        )
    legacy = replace(managed, max_duration_seconds=MAX_NEW_MANAGED_DURATION_SECONDS + 1)
    store.save(ManagedState(playlists=(legacy,)))
    round_tripped = service.update(
        legacy.id,
        display_name=legacy.display_name,
        description=legacy.description,
        cover_id=legacy.cover_id,
        source_ids=legacy.source_ids,
        enabled=True,
        access_token=None,
        max_duration_seconds=legacy.max_duration_seconds,
    )
    assert round_tripped.max_duration_seconds == MAX_NEW_MANAGED_DURATION_SECONDS + 1
