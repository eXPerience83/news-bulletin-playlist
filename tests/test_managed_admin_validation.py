from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_admin import (
    MAX_PLAYLIST_DESCRIPTION_LENGTH,
    MAX_PLAYLIST_NAME_LENGTH,
    ManagedAdminError,
    ManagedAdminService,
)
from news_bulletin_playlist.managed_admin_web import render_managed_admin_page
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.spotify.auth import AuthorizationState


class _FakeClient:
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


class _Factory:
    def __init__(self) -> None:
        self.client = _FakeClient()
        self.tokens: list[str] = []

    def __call__(self, access_token: str) -> _FakeClient:
        self.tokens.append(access_token)
        return self.client


def _service(tmp_path: Path) -> tuple[ManagedAdminService, _Factory]:
    factory = _Factory()
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=factory,
    )
    return service, factory


def _activate(service: ManagedAdminService) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="create-token",
    )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("display_name", "x" * (MAX_PLAYLIST_NAME_LENGTH + 1), "playlist name"),
        (
            "description",
            "x" * (MAX_PLAYLIST_DESCRIPTION_LENGTH + 1),
            "playlist description",
        ),
        ("cover_id", "not-a-bundled-cover", "unknown bundled cover"),
    ],
)
def test_invalid_product_fields_are_rejected_before_spotify_write(
    tmp_path: Path,
    field: str,
    value: str,
    match: str,
) -> None:
    service, factory = _service(tmp_path)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    values = {
        "display_name": template.display_name,
        "description": template.description,
        "cover_id": template.cover_id,
    }
    values[field] = value

    with pytest.raises(ManagedAdminError, match=match):
        service.activate(
            template_id=template.id,
            display_name=values["display_name"],
            description=values["description"],
            cover_id=values["cover_id"],
            source_ids=template.default_source_ids,
            access_token="must-not-be-used",
        )

    assert factory.tokens == []
    assert factory.client.create_calls == []


def test_source_only_edit_remains_available_without_spotify_token(tmp_path: Path) -> None:
    service, factory = _service(tmp_path)
    _activate(service)
    current = service.snapshot().managed[0]

    updated = service.update(
        current.id,
        display_name=current.display_name,
        description=current.description,
        cover_id=current.cover_id,
        source_ids=("rne",),
        enabled=True,
        access_token=None,
    )

    assert updated.source_ids == (SourceId("rne"),)
    assert factory.client.update_calls == []
    assert factory.tokens == ["create-token"]


def test_dashboard_uses_service_limits_and_keeps_destination_server_owned(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    _activate(service)

    body = render_managed_admin_page(
        snapshot=service.snapshot(),
        catalog=BUILTIN_CATALOG,
        spotify_state=AuthorizationState.CONNECTED,
        csrf_token="csrf-token",
        last_cycle=None,
        lan_mode=True,
    ).decode()

    assert f'maxlength="{MAX_PLAYLIST_NAME_LENGTH}"' in body
    assert f'maxlength="{MAX_PLAYLIST_DESCRIPTION_LENGTH}"' in body
    assert 'name="destination_id"' not in body
    assert "/admin/covers/spain_spanish_news.jpg" in body
    assert "Cadena SER" in body
    assert "Radio Nacional de España" in body
    assert "Onda Cero" in body
    assert "CNN 5 Cosas" in body
    assert "LAN development mode" in body


def test_available_template_requires_spotify_but_local_save_button_does_not(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    disconnected = render_managed_admin_page(
        snapshot=service.snapshot(),
        catalog=BUILTIN_CATALOG,
        spotify_state=AuthorizationState.DISCONNECTED,
        csrf_token="csrf-token",
        last_cycle=None,
        lan_mode=True,
    ).decode()
    assert '<button type="submit" disabled>Create private playlist</button>' in disconnected
    assert "Connect Spotify before activation" in disconnected

    _activate(service)
    managed = render_managed_admin_page(
        snapshot=service.snapshot(),
        catalog=BUILTIN_CATALOG,
        spotify_state=AuthorizationState.DISCONNECTED,
        csrf_token="csrf-token-2",
        last_cycle=None,
        lan_mode=True,
    ).decode()
    assert '<button type="submit">Save playlist</button>' in managed
