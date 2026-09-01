from __future__ import annotations

import base64
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPMessage
from pathlib import Path
from typing import Any

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine import OperationalStatus
from news_bulletin_playlist.engine_runtime import (
    ConfigurationSynchronization,
    OperationalHealthHandler,
)
from news_bulletin_playlist.lan_admin import LanAdminSecurity
from news_bulletin_playlist.managed_admin import ManagedAdminService, render_spotify_description
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.spotify.auth import AuthorizationState

_PASSWORD = "long-enough-admin-password"


class _SpotifyClient:
    def __init__(self) -> None:
        self.update_calls: list[tuple[str, str, str]] = []
        self.cover_calls: list[tuple[str, bytes]] = []
        self.block_updates = False
        self.update_started = threading.Event()
        self.update_release = threading.Event()

    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:
        del name, description
        return {"id": "destination"}

    def change_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        self.update_calls.append((playlist_id, name, description))
        if self.block_updates:
            self.update_started.set()
            if not self.update_release.wait(timeout=5.0):
                raise AssertionError("timed out waiting to release Spotify metadata update")
        return {}

    def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        self.cover_calls.append((playlist_id, jpeg_bytes))
        return {}


class _Factory:
    def __init__(self, client: _SpotifyClient) -> None:
        self.client = client

    def __call__(self, token: str) -> _SpotifyClient:
        assert token in {"setup-token", "sync-token"}
        return self.client


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    def get_access_token(self, *, now: object = None) -> str:
        del now
        self.calls += 1
        return "sync-token"


class _SpotifyState:
    def authorization_state(self) -> AuthorizationState:
        return AuthorizationState.CONNECTED


class _Lifecycle:
    def __init__(self) -> None:
        self.status = OperationalStatus(configured=True)
        self.scheduler = None
        self.reconcile_calls: list[bool] = []

    def reconcile(self, *, configured: bool) -> None:
        self.reconcile_calls.append(configured)

    def wake(self) -> None:
        return


@dataclass(frozen=True, slots=True)
class _Response:
    status: HTTPStatus
    payload: bytes
    extra_headers: dict[str, str]


class _Handler(OperationalHealthHandler):
    def __init__(
        self,
        *,
        tmp_path: Path,
        service: ManagedAdminService,
        lifecycle: _Lifecycle,
        provider: _Provider,
        security: LanAdminSecurity,
        path: str,
        form: dict[str, list[str]] | None = None,
    ) -> None:
        self.data_dir = tmp_path
        self.admin_security = security
        self.spotify_auth = _SpotifyState()  # type: ignore[assignment]
        self.operational_status = lifecycle.status
        self.engine_scheduler = None
        self.engine_lifecycle = lifecycle  # type: ignore[assignment]
        self.auth_synchronization = None
        self.configuration_synchronization = ConfigurationSynchronization()
        self.managed_admin_service = service
        self.managed_admin_auth = provider
        self.path = path
        self.client_address = ("127.0.0.1", 12345)
        self.headers = HTTPMessage()
        encoded = base64.b64encode(f"admin:{_PASSWORD}".encode()).decode("ascii")
        self.headers["Authorization"] = f"Basic {encoded}"
        self._form = {} if form is None else form
        self.response: _Response | None = None

    def _read_form(self) -> dict[str, list[str]]:
        return self._form

    def _reply(
        self,
        status: HTTPStatus,
        payload: bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        del content_type
        self.response = _Response(
            status=status,
            payload=payload,
            extra_headers={} if extra_headers is None else dict(extra_headers),
        )


def _managed_service(tmp_path: Path) -> tuple[ManagedAdminService, _SpotifyClient, object]:
    client = _SpotifyClient()
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=_Factory(client),
        cover_loader=lambda _cover_id: b"\xff\xd8cover\xff\xd9",
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="setup-token",
    )
    client.update_calls.clear()
    client.cover_calls.clear()
    return service, client, managed


def test_admin_dashboard_exposes_explicit_metadata_and_cover_action(tmp_path: Path) -> None:
    service, _, _ = _managed_service(tmp_path)
    lifecycle = _Lifecycle()
    security = LanAdminSecurity(_PASSWORD)
    handler = _Handler(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        provider=_Provider(),
        security=security,
        path="/admin/",
    )

    handler.do_GET()

    assert handler.response is not None
    body = handler.response.payload.decode()
    assert handler.response.status == HTTPStatus.OK
    assert 'action="/admin/playlists/sync"' in body
    assert "Apply Spotify metadata &amp; cover" in body
    assert "Reconnect Spotify once for image permission" in body


def test_explicit_admin_sync_uses_token_and_does_not_restart_scheduler(tmp_path: Path) -> None:
    service, client, managed = _managed_service(tmp_path)
    lifecycle = _Lifecycle()
    provider = _Provider()
    security = LanAdminSecurity(_PASSWORD)
    handler = _Handler(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        provider=provider,
        security=security,
        path="/admin/playlists/sync",
        form={
            "csrf_token": [security.issue_csrf_token()],
            "playlist_id": [str(managed.id)],
        },
    )

    handler.do_POST()

    assert handler.response is not None
    assert handler.response.status == HTTPStatus.SEE_OTHER
    assert handler.response.extra_headers["Location"] == "/admin/"
    assert provider.calls == 1
    assert client.update_calls == [
        (
            "destination",
            managed.display_name,
            render_spotify_description(managed.description),
        )
    ]
    assert client.cover_calls == [("destination", b"\xff\xd8cover\xff\xd9")]
    assert lifecycle.reconcile_calls == []


def test_explicit_admin_sync_releases_configuration_lock_during_spotify_io(
    tmp_path: Path,
) -> None:
    service, client, managed = _managed_service(tmp_path)
    lifecycle = _Lifecycle()
    provider = _Provider()
    security = LanAdminSecurity(_PASSWORD)
    handler = _Handler(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        provider=provider,
        security=security,
        path="/admin/playlists/sync",
        form={
            "csrf_token": [security.issue_csrf_token()],
            "playlist_id": [str(managed.id)],
        },
    )
    synchronization = handler.configuration_synchronization
    assert synchronization is not None
    client.block_updates = True

    sync_thread = threading.Thread(target=handler.do_POST)
    sync_thread.start()
    assert client.update_started.wait(timeout=1.0)

    cycle_acquired = threading.Event()

    def acquire_for_engine_cycle() -> None:
        with synchronization.hold():
            cycle_acquired.set()

    cycle_thread = threading.Thread(target=acquire_for_engine_cycle)
    cycle_thread.start()
    try:
        assert cycle_acquired.wait(timeout=1.0)
    finally:
        client.update_release.set()
        sync_thread.join(timeout=2.0)
        cycle_thread.join(timeout=2.0)

    assert not sync_thread.is_alive()
    assert not cycle_thread.is_alive()
    assert handler.response is not None
    assert handler.response.status == HTTPStatus.SEE_OTHER
    assert lifecycle.reconcile_calls == []
