from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.client import HTTPMessage
from pathlib import Path
from typing import Any

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine import EngineCycleResult, OperationalStatus
from news_bulletin_playlist.engine_runtime import (
    ConfigurationSynchronization,
    EngineLifecycleController,
    OperationalHealthHandler,
    _build_managed_admin_service,
)
from news_bulletin_playlist.lan_admin import LanAdminSecurity
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.spotify.auth import AuthorizationState

NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
_PASSWORD = "long-enough-admin-password"


class _FakeSpotifyClient:
    def __init__(self, destination_id: str = "spotify-destination") -> None:
        self.destination_id = destination_id
        self.create_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, str, str]] = []

    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:
        self.create_calls.append((name, description))
        return {"id": self.destination_id}

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
    def __init__(self, client: _FakeSpotifyClient) -> None:
        self.client = client
        self.tokens: list[str] = []

    def __call__(self, access_token: str) -> _FakeSpotifyClient:
        self.tokens.append(access_token)
        return self.client


class _AccessTokenProvider:
    def __init__(self, token: str = "access-token-sentinel") -> None:
        self.token = token
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        del now
        self.calls += 1
        return self.token


class _SpotifyState:
    def __init__(self, state: AuthorizationState) -> None:
        self.state = state

    def authorization_state(self) -> AuthorizationState:
        return self.state


class _FakeLifecycle:
    def __init__(self) -> None:
        self.reconcile_calls: list[bool] = []
        self.wake_calls = 0
        self.scheduler = None
        self.status = OperationalStatus(configured=False)

    def reconcile(self, *, configured: bool) -> None:
        self.reconcile_calls.append(configured)

    def wake(self) -> None:
        self.wake_calls += 1


@dataclass(frozen=True, slots=True)
class _CapturedResponse:
    status: HTTPStatus
    payload: bytes
    content_type: str
    extra_headers: dict[str, str]


class _HandlerHarness(OperationalHealthHandler):
    """Exercise managed admin routes entirely in memory without TCP sockets."""

    def __init__(
        self,
        *,
        tmp_path: Path,
        service: ManagedAdminService,
        lifecycle: _FakeLifecycle,
        path: str,
        form: dict[str, list[str]] | None = None,
        password: str | None = _PASSWORD,
        spotify_state: AuthorizationState = AuthorizationState.CONNECTED,
        auth_provider: _AccessTokenProvider | None = None,
        security: LanAdminSecurity | None = None,
    ) -> None:
        self.data_dir = tmp_path
        self.admin_security = security or LanAdminSecurity(_PASSWORD)
        self.spotify_auth = _SpotifyState(spotify_state)  # type: ignore[assignment]
        self.operational_status = lifecycle.status
        self.engine_scheduler = None
        self.engine_lifecycle = lifecycle  # type: ignore[assignment]
        self.auth_synchronization = None
        self.configuration_synchronization = ConfigurationSynchronization()
        self.managed_admin_service = service
        self.managed_admin_auth = auth_provider
        self.path = path
        self.client_address = ("127.0.0.1", 12345)
        self.headers = HTTPMessage()
        if password is not None:
            self.headers["Authorization"] = _basic_header(password)
        self._form = {} if form is None else form
        self.response: _CapturedResponse | None = None

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
        self.response = _CapturedResponse(
            status=status,
            payload=payload,
            content_type=content_type,
            extra_headers={} if extra_headers is None else dict(extra_headers),
        )


class _CycleRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.called = threading.Event()

    def run_cycle(self) -> EngineCycleResult:
        self.calls += 1
        self.called.set()
        return EngineCycleResult(
            started_at=NOW,
            finished_at=NOW,
            ok=True,
            sources=(),
            playlists=(),
        )


def _basic_header(password: str) -> str:
    encoded = base64.b64encode(f"admin:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


def _service(tmp_path: Path) -> tuple[ManagedAdminService, _Factory]:
    client = _FakeSpotifyClient()
    factory = _Factory(client)
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=factory,
    )
    return service, factory


def _response(handler: _HandlerHarness) -> _CapturedResponse:
    assert handler.response is not None
    return handler.response


def _activation_form(csrf_token: str) -> dict[str, list[str]]:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    return {
        "csrf_token": [csrf_token],
        "template_id": [str(template.id)],
        "display_name": [template.display_name],
        "description": [template.description],
        "cover_id": [template.cover_id],
        "source_id": [str(source_id) for source_id in template.default_source_ids],
    }


def _activate_direct(service: ManagedAdminService) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="setup-token",
    )


def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_dashboard_requires_auth_and_exposes_available_template(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    lifecycle = _FakeLifecycle()

    unauthorized = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/",
        password=None,
    )
    unauthorized.do_GET()
    assert _response(unauthorized).status == HTTPStatus.UNAUTHORIZED

    authorized = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/",
    )
    authorized.do_GET()
    body = _response(authorized).payload.decode()
    assert _response(authorized).status == HTTPStatus.OK
    assert "Active playlists" in body
    assert "Available playlists" in body
    assert "Noticias España" in body
    assert "Cadena SER" in body
    assert "Radio Nacional de España" in body
    assert "Onda Cero" in body
    assert "CNN 5 Cosas" in body
    assert "/admin/covers/spain_spanish_news.jpg" in body


def test_activation_is_csrf_protected_persists_once_and_requests_scheduler_start(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    lifecycle = _FakeLifecycle()
    provider = _AccessTokenProvider()
    security = LanAdminSecurity(_PASSWORD)
    csrf_token = security.issue_csrf_token()
    form = _activation_form(csrf_token)

    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/activate",
        form=form,
        auth_provider=provider,
        security=security,
    )
    handler.do_POST()

    response = _response(handler)
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/"
    assert provider.calls == 1
    assert factory.tokens == ["access-token-sentinel"]
    assert len(factory.client.create_calls) == 1
    assert len(service.snapshot().managed) == 1
    assert lifecycle.reconcile_calls == [True]
    raw_state = (tmp_path / "managed-state.json").read_text()
    assert "access-token-sentinel" not in raw_state
    assert "access-token-sentinel" not in response.payload.decode()

    replay = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/activate",
        form=form,
        auth_provider=provider,
        security=security,
    )
    replay.do_POST()
    assert _response(replay).status == HTTPStatus.FORBIDDEN
    assert provider.calls == 1
    assert len(factory.client.create_calls) == 1


def test_source_only_edit_works_offline_and_does_not_write_spotify_metadata(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    _activate_direct(service)
    current = service.snapshot().managed[0]
    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    csrf_token = security.issue_csrf_token()
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/update",
        form={
            "csrf_token": [csrf_token],
            "playlist_id": [str(current.id)],
            "display_name": [current.display_name],
            "description": [current.description],
            "cover_id": [current.cover_id],
            "source_id": ["rne"],
            "enabled": ["1"],
        },
        spotify_state=AuthorizationState.REAUTH_REQUIRED,
        auth_provider=None,
        security=security,
    )
    handler.do_POST()

    assert _response(handler).status == HTTPStatus.SEE_OTHER
    assert service.snapshot().managed[0].source_ids == (SourceId("rne"),)
    assert factory.client.update_calls == []
    assert lifecycle.reconcile_calls == [True]


def test_metadata_edit_without_spotify_token_fails_without_changing_local_state(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    _activate_direct(service)
    current = service.snapshot().managed[0]
    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/update",
        form={
            "csrf_token": [security.issue_csrf_token()],
            "playlist_id": [str(current.id)],
            "display_name": ["Nuevo nombre"],
            "description": [current.description],
            "cover_id": [current.cover_id],
            "source_id": [str(source_id) for source_id in current.source_ids],
            "enabled": ["1"],
        },
        spotify_state=AuthorizationState.REAUTH_REQUIRED,
        auth_provider=None,
        security=security,
    )
    handler.do_POST()

    response = _response(handler)
    assert response.status == HTTPStatus.CONFLICT
    assert "Spotify must be connected" in response.payload.decode()
    assert service.snapshot().managed[0] == current
    assert factory.client.update_calls == []
    assert lifecycle.reconcile_calls == []


def test_stop_managing_keeps_spotify_destination_and_requests_scheduler_stop(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    _activate_direct(service)
    current = service.snapshot().managed[0]
    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/stop",
        form={
            "csrf_token": [security.issue_csrf_token()],
            "playlist_id": [str(current.id)],
        },
        auth_provider=None,
        security=security,
    )
    handler.do_POST()

    assert _response(handler).status == HTTPStatus.SEE_OTHER
    assert service.snapshot().managed == ()
    assert lifecycle.reconcile_calls == [False]
    assert factory.client.create_calls
    assert factory.client.update_calls == []


def test_bundled_cover_route_is_authenticated_and_rejects_unknown_ids(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    lifecycle = _FakeLifecycle()

    known = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/covers/spain_spanish_news.jpg",
    )
    known.do_GET()
    response = _response(known)
    assert response.status == HTTPStatus.OK
    assert response.content_type == "image/jpeg"
    assert response.payload.startswith(b"\xff\xd8")

    unknown = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/covers/unknown.jpg",
    )
    unknown.do_GET()
    assert _response(unknown).status == HTTPStatus.NOT_FOUND

    unauthorized = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/covers/spain_spanish_news.jpg",
        password=None,
    )
    unauthorized.do_GET()
    assert _response(unauthorized).status == HTTPStatus.UNAUTHORIZED


def test_lifecycle_starts_stops_and_can_start_again_without_idle_cycles() -> None:
    runner = _CycleRunner()
    controller = EngineLifecycleController(  # type: ignore[arg-type]
        runner,
        interval=timedelta(hours=1),
    )
    assert controller.scheduler is None
    assert controller.status.snapshot().configured is False
    assert runner.calls == 0

    controller.reconcile(configured=True)
    assert runner.called.wait(timeout=2)
    _wait_until(lambda: controller.status.snapshot().next_run_at is not None)
    first_scheduler = controller.scheduler
    assert first_scheduler is not None
    assert controller.status.snapshot().configured is True

    controller.reconcile(configured=False)
    assert controller.scheduler is None
    stopped_calls = runner.calls
    time.sleep(0.05)
    assert runner.calls == stopped_calls
    stopped_snapshot = controller.status.snapshot()
    assert stopped_snapshot.configured is False
    assert stopped_snapshot.next_run_at is None

    runner.called.clear()
    controller.reconcile(configured=True)
    assert runner.called.wait(timeout=2)
    assert controller.scheduler is not None
    assert controller.scheduler is not first_scheduler
    assert runner.calls == stopped_calls + 1

    controller.shutdown()
    assert controller.scheduler is None
    assert controller.status.snapshot().configured is False


def test_legacy_or_explicit_yaml_disables_managed_web_service(tmp_path: Path) -> None:
    spotify_auth = object()
    assert _build_managed_admin_service(  # type: ignore[arg-type]
        tmp_path,
        {"NEWS_PLAYLIST_CONFIG": "/data/manual.yaml"},
        spotify_auth=spotify_auth,
    ) is None

    (tmp_path / "news-bulletin-playlist.yaml").write_text("legacy")
    assert _build_managed_admin_service(  # type: ignore[arg-type]
        tmp_path,
        {},
        spotify_auth=spotify_auth,
    ) is None



class _TrackingHold:
    def __init__(self, synchronization: _TrackingSynchronization) -> None:
        self.synchronization = synchronization

    def __enter__(self) -> None:
        self.synchronization.depth += 1

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.synchronization.depth -= 1


class _TrackingSynchronization:
    def __init__(self) -> None:
        self.depth = 0

    def hold(self) -> _TrackingHold:
        return _TrackingHold(self)


class _AssertUnlockedLifecycle(_FakeLifecycle):
    def __init__(self, synchronization: _TrackingSynchronization) -> None:
        super().__init__()
        self.synchronization = synchronization

    def reconcile(self, *, configured: bool) -> None:
        assert self.synchronization.depth == 0
        super().reconcile(configured=configured)


def test_lifecycle_reconcile_runs_after_configuration_lock_is_released(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    _activate_direct(service)
    current = service.snapshot().managed[0]
    synchronization = _TrackingSynchronization()
    lifecycle = _AssertUnlockedLifecycle(synchronization)
    security = LanAdminSecurity(_PASSWORD)
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/stop",
        form={
            "csrf_token": [security.issue_csrf_token()],
            "playlist_id": [str(current.id)],
        },
        auth_provider=None,
        security=security,
    )
    handler.configuration_synchronization = synchronization  # type: ignore[assignment]

    handler.do_POST()

    assert _response(handler).status == HTTPStatus.SEE_OTHER
    assert synchronization.depth == 0
    assert lifecycle.reconcile_calls == [False]
