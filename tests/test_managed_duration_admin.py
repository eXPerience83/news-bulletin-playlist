from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from _html import assert_valid_admin_html

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_admin import ManagedAdminError, ManagedAdminService
from news_bulletin_playlist.managed_admin_web import (
    duration_minutes_from_form,
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


def test_duration_form_accepts_whole_minutes_and_rejects_invalid_values() -> None:
    assert duration_minutes_from_form({}, "max") is None
    assert duration_minutes_from_form({"max_duration_minutes": ["30"]}, "max") == 30
    assert duration_minutes_from_form({"min_duration_minutes": ["0"]}, "min") == 0
    for value in ["-1", "30.5"]:
        with pytest.raises(ValueError):
            duration_minutes_from_form({"max_duration_minutes": [value]}, "max")
    with pytest.raises(ValueError, match="Exactly one"):
        duration_minutes_from_form({"min_duration_minutes": ["0", "1"]}, "min")


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
        state=service.store.load(),
        catalog=BUILTIN_CATALOG,
        spotify_state=AuthorizationState.CONNECTED,
        csrf_token="csrf",
        last_cycle=None,
        lan_mode=True,
    ).decode("utf-8")

    assert_valid_admin_html(page)
    assert 'name="min_duration_minutes"' in page
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
        state=service.store.load(),
        catalog=BUILTIN_CATALOG,
        spotify_state=AuthorizationState.CONNECTED,
        csrf_token="csrf",
        last_cycle=None,
        lan_mode=True,
    ).decode()
    assert_valid_admin_html(page)
    assert 'name="max_duration_minutes" required min="0"' in page
    assert 'value="20"' in page
    assert 'name="max_duration_seconds_remainder"' not in page
    assert "Historical precision in seconds" in page
    current = service.snapshot().managed[0]
    updated = service.update(
        current.id,
        display_name=current.display_name,
        description=current.description,
        cover_id=current.cover_id,
        source_ids=current.source_ids,
        enabled=True,
        access_token=None,
        max_duration_minutes=20,
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


@pytest.mark.parametrize("bound", ["min", "max"])
@pytest.mark.parametrize("minutes,expected", [(20, 1234), (21, 1260)])
def test_symmetric_legacy_minutes_round_trip(tmp_path, bound, minutes, expected):
    store = ManagedStateStore(tmp_path / "managed-state.json")
    playlist = replace(
        activate_template(BUILTIN_CATALOG.playlists[0], "destination"),
        min_duration_seconds=1234,
        max_duration_seconds=1234,
    )
    store.save(ManagedState(playlists=(playlist,)))
    factory = _Factory()
    service = ManagedAdminService(store, client_factory=factory)
    # Move both bounds together so the candidate remains valid whichever bound is tested.
    updated = service.update(
        playlist.id,
        display_name=playlist.display_name,
        description=playlist.description,
        cover_id=playlist.cover_id,
        source_ids=playlist.source_ids,
        enabled=True,
        access_token=None,
        min_duration_minutes=minutes,
        max_duration_minutes=minutes,
    )
    assert getattr(updated, f"{bound}_duration_seconds") == expected
    assert factory.tokens == []
    assert factory.client.update_calls == []


def test_candidate_pair_is_atomic_and_invalid_range_preserves_document(tmp_path):
    store = ManagedStateStore(tmp_path / "managed-state.json")
    playlist = activate_template(BUILTIN_CATALOG.playlists[0], "destination")
    store.save(ManagedState(playlists=(playlist,)))
    service = ManagedAdminService(store, client_factory=_Factory())
    kwargs = dict(
        display_name=playlist.display_name,
        description=playlist.description,
        cover_id=playlist.cover_id,
        source_ids=playlist.source_ids,
        enabled=True,
        access_token=None,
    )
    updated = service.update(
        playlist.id, **kwargs, min_duration_seconds=3600, max_duration_seconds=7200
    )
    assert (updated.min_duration_seconds, updated.max_duration_seconds) == (3600, 7200)
    updated = service.update(
        playlist.id, **kwargs, min_duration_seconds=600, max_duration_seconds=1800
    )
    assert (updated.min_duration_seconds, updated.max_duration_seconds) == (600, 1800)
    original = store.path.read_bytes()
    with pytest.raises(ManagedAdminError, match="minimum.*maximum"):
        service.update(playlist.id, **kwargs, min_duration_seconds=1801)
    assert store.path.read_bytes() == original


def test_historical_over_cap_range_preserved_but_new_minimum_rejected(tmp_path):
    store = ManagedStateStore(tmp_path / "managed-state.json")
    playlist = replace(
        activate_template(BUILTIN_CATALOG.playlists[0], "destination"),
        min_duration_seconds=86461,
        max_duration_seconds=86521,
    )
    store.save(ManagedState(playlists=(playlist,)))
    service = ManagedAdminService(store, client_factory=_Factory())
    kwargs = dict(
        display_name=playlist.display_name,
        description=playlist.description,
        cover_id=playlist.cover_id,
        source_ids=playlist.source_ids,
        enabled=True,
        access_token=None,
    )
    assert (
        service.update(playlist.id, **kwargs, min_duration_minutes=1441, max_duration_minutes=1442)
        == playlist
    )
    with pytest.raises(ManagedAdminError, match="at most"):
        service.update(playlist.id, **kwargs, min_duration_seconds=86462)


def test_activation_snapshots_nonzero_template_range(tmp_path):
    from news_bulletin_playlist.catalog import BuiltInCatalog
    from news_bulletin_playlist.models import DurationPolicy

    template = replace(
        BUILTIN_CATALOG.playlists[0],
        duration_policy=DurationPolicy(default_min_seconds=634, default_max_seconds=1234),
    )
    catalog = BuiltInCatalog(sources=BUILTIN_CATALOG.sources, playlists=(template,))
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "state.json"), catalog=catalog, client_factory=_Factory()
    )
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="test",
        min_duration_minutes=10,
        max_duration_minutes=20,
    )
    assert (managed.min_duration_seconds, managed.max_duration_seconds) == (634, 1234)


def test_subminute_legacy_maximum_round_trips_zero_visible_minutes(tmp_path):
    store = ManagedStateStore(tmp_path / "state.json")
    playlist = replace(
        activate_template(BUILTIN_CATALOG.playlists[0], "destination"), max_duration_seconds=34
    )
    store.save(ManagedState(playlists=(playlist,)))
    service = ManagedAdminService(store, client_factory=_Factory())
    updated = service.update(
        playlist.id,
        display_name=playlist.display_name,
        description=playlist.description,
        cover_id=playlist.cover_id,
        source_ids=playlist.source_ids,
        enabled=True,
        access_token=None,
        max_duration_minutes=0,
    )
    assert updated.max_duration_seconds == 34
