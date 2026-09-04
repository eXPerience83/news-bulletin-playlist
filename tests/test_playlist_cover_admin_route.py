from __future__ import annotations

import base64
import io
import logging
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPMessage
from pathlib import Path
from typing import Any

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import OperationalStatus
from news_bulletin_playlist.engine_runtime import (
    ConfigurationSynchronization,
    OperationalHealthHandler,
)
from news_bulletin_playlist.lan_admin import LanAdminSecurity
from news_bulletin_playlist.managed_admin import ManagedAdminService, render_spotify_description
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify.auth import AuthorizationState
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyTransportError,
)

_PASSWORD = "long-enough-admin-password"


class _SpotifyClient:
    def __init__(self) -> None:
        self.update_calls: list[tuple[str, str, str]] = []
        self.cover_calls: list[tuple[str, bytes]] = []
        self.block_updates = False
        self.metadata_error: Exception | None = None
        self.cover_error: Exception | None = None
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
        if self.metadata_error is not None:
            raise self.metadata_error
        return {}

    def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        self.cover_calls.append((playlist_id, jpeg_bytes))
        if self.cover_error is not None:
            raise self.cover_error
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
        diagnostic_store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite")
        diagnostic_store.initialize()
        self.diagnostic_store = diagnostic_store
        self.diagnostic_output = io.StringIO()
        diagnostic_logger = logging.Logger("test-admin-diagnostics")
        diagnostic_logger.addHandler(logging.StreamHandler(self.diagnostic_output))
        self.operational_diagnostics = OperationalDiagnostics(
            diagnostic_store, logger=diagnostic_logger
        )
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
    assert "Spotify metadata/cover sync is independent from bulletin policy changes." in body


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
    assert handler.response.extra_headers["Location"] == "/admin/?notice=spotify-sync-applied"
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
    assert handler.diagnostic_store is not None
    events = handler.diagnostic_store.list_events(limit=10)
    admin_events = [event for event in events if event.component == "admin"]
    by_operation = {event.details["operation"]: event for event in admin_events}
    assert by_operation["playlist_metadata"].playlist_id == str(managed.id)
    assert by_operation["playlist_metadata"].details == {
        "operation": "playlist_metadata",
        "outcome": "applied",
    }
    assert by_operation["playlist_cover_upload"].details == {
        "operation": "playlist_cover_upload",
        "outcome": "applied",
    }


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


def test_explicit_admin_sync_reports_metadata_http_status_and_still_attempts_cover(
    tmp_path: Path,
    capsys,
) -> None:
    service, client, managed = _managed_service(tmp_path)
    lifecycle = _Lifecycle()
    provider = _Provider()
    security = LanAdminSecurity(_PASSWORD)
    client.metadata_error = SpotifyApiError(403, "access-token-sentinel")
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
    body = handler.response.payload.decode()
    assert handler.response.status == HTTPStatus.BAD_GATEWAY
    assert "metadata failed (HTTP 403); cover applied" in body
    assert "access-token-sentinel" not in body
    assert client.cover_calls == [("destination", b"\xff\xd8cover\xff\xd9")]
    captured = capsys.readouterr()
    assert "access-token-sentinel" not in captured.out
    assert "access-token-sentinel" not in captured.err
    diagnostic_output = handler.diagnostic_output.getvalue()
    assert "event=admin_spotify_operation_completed" in diagnostic_output
    assert "access-token-sentinel" not in diagnostic_output
    assert lifecycle.reconcile_calls == []
    assert handler.diagnostic_store is not None
    events = handler.diagnostic_store.list_events(limit=10)
    by_operation = {
        event.details["operation"]: event for event in events if event.component == "admin"
    }
    assert by_operation["playlist_metadata"].details == {
        "failure_class": "api_error",
        "http_status": 403,
        "operation": "playlist_metadata",
        "outcome": "failed",
    }
    assert by_operation["playlist_cover_upload"].details == {
        "operation": "playlist_cover_upload",
        "outcome": "applied",
    }


def test_explicit_admin_sync_reports_cover_network_failure_without_raw_error(
    tmp_path: Path,
    capsys,
) -> None:
    service, client, managed = _managed_service(tmp_path)
    lifecycle = _Lifecycle()
    provider = _Provider()
    security = LanAdminSecurity(_PASSWORD)
    client.cover_error = SpotifyTransportError("refresh-token-sentinel")
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
    body = handler.response.payload.decode()
    assert handler.response.status == HTTPStatus.BAD_GATEWAY
    assert "metadata applied; cover failed (network error)" in body
    assert "refresh-token-sentinel" not in body
    captured = capsys.readouterr()
    assert "refresh-token-sentinel" not in captured.out
    assert "refresh-token-sentinel" not in captured.err
    diagnostic_output = handler.diagnostic_output.getvalue()
    assert "event=admin_spotify_operation_completed" in diagnostic_output
    assert "refresh-token-sentinel" not in diagnostic_output
    assert lifecycle.reconcile_calls == []
    assert handler.diagnostic_store is not None
    events = handler.diagnostic_store.list_events(limit=10)
    by_operation = {
        event.details["operation"]: event for event in events if event.component == "admin"
    }
    assert by_operation["playlist_metadata"].details == {
        "operation": "playlist_metadata",
        "outcome": "applied",
    }
    assert by_operation["playlist_cover_upload"].details == {
        "failure_class": "transport_error",
        "operation": "playlist_cover_upload",
        "outcome": "failed",
    }


def test_admin_update_and_stop_leave_sanitized_audit_events(tmp_path: Path) -> None:
    service, _, managed = _managed_service(tmp_path)
    lifecycle = _Lifecycle()
    provider = _Provider()
    security = LanAdminSecurity(_PASSWORD)
    update_handler = _Handler(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        provider=provider,
        security=security,
        path="/admin/playlists/update",
        form={
            "csrf_token": [security.issue_csrf_token()],
            "playlist_id": [str(managed.id)],
            "display_name": [managed.display_name],
            "description": [managed.description],
            "cover_id": [managed.cover_id],
            "source_id": [str(source_id) for source_id in managed.source_ids],
            "enabled": ["1"],
        },
    )

    update_handler.do_POST()

    assert update_handler.response is not None
    assert update_handler.response.status == HTTPStatus.SEE_OTHER
    assert update_handler.diagnostic_store is not None
    update_event = update_handler.diagnostic_store.list_events(limit=1)[0]
    assert update_event.event_name == "admin_playlist_updated"
    assert update_event.playlist_id == str(managed.id)
    assert update_event.details == {
        "next_state": "enabled",
        "operation": "playlist_configuration",
        "outcome": "applied",
    }

    stop_handler = _Handler(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        provider=provider,
        security=security,
        path="/admin/playlists/stop",
        form={
            "csrf_token": [security.issue_csrf_token()],
            "playlist_id": [str(managed.id)],
        },
    )

    stop_handler.do_POST()

    assert stop_handler.response is not None
    assert stop_handler.response.status == HTTPStatus.SEE_OTHER
    assert stop_handler.diagnostic_store is not None
    stop_event = stop_handler.diagnostic_store.list_events(limit=1)[0]
    assert stop_event.event_name == "admin_playlist_stopped"
    assert stop_event.playlist_id == str(managed.id)
    assert stop_event.details == {
        "next_state": "stopped",
        "operation": "playlist_configuration",
        "outcome": "applied",
    }


def test_admin_dashboard_shows_sync_success_notice(tmp_path: Path) -> None:
    service, _, _ = _managed_service(tmp_path)
    lifecycle = _Lifecycle()
    security = LanAdminSecurity(_PASSWORD)
    handler = _Handler(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        provider=_Provider(),
        security=security,
        path="/admin/?notice=spotify-sync-applied",
    )

    handler.do_GET()

    assert handler.response is not None
    body = handler.response.payload.decode()
    assert handler.response.status == HTTPStatus.OK
    assert "Spotify metadata and cover applied successfully." in body
    assert 'class="notice"' in body


def test_admin_dashboard_ignores_unknown_notice_text(tmp_path: Path) -> None:
    service, _, _ = _managed_service(tmp_path)
    lifecycle = _Lifecycle()
    security = LanAdminSecurity(_PASSWORD)
    sentinel = "script-alert-sentinel"
    handler = _Handler(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        provider=_Provider(),
        security=security,
        path=f"/admin/?notice={sentinel}",
    )

    handler.do_GET()

    assert handler.response is not None
    body = handler.response.payload.decode()
    assert handler.response.status == HTTPStatus.OK
    assert sentinel not in body
    assert "Spotify metadata and cover applied successfully." not in body
