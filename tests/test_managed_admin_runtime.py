from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _http_handler import (
    HandlerCaptureMixin,
    initialize_handler_capture,
)
from _http_handler import (
    captured_response as _response,
)

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine import (
    EngineCycleResult,
    EngineRunner,
    OperationalStatus,
    _required_sources,
)
from news_bulletin_playlist.engine_runtime import (
    ConfigurationSynchronization,
    EngineLifecycleController,
    OperationalHealthHandler,
    _build_managed_admin_service,
)
from news_bulletin_playlist.lan_admin import LanAdminSecurity
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_provisioning import (
    ProvisioningIntent,
    ProvisioningJournal,
    ProvisioningState,
)
from news_bulletin_playlist.managed_state import (
    ManagedSource,
    ManagedState,
    ManagedStateStore,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.models import (
    CountryCode,
    EditorialScope,
    LanguageTag,
    ParserId,
    SourceId,
    SourceOrigin,
)
from news_bulletin_playlist.spotify.auth import AuthorizationState
from news_bulletin_playlist.spotify.client import SpotifyTransportError

NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
_PASSWORD = "long-enough-admin-password"


class _FakeSpotifyClient:
    def __init__(self, destination_id: str = "spotify-destination") -> None:
        self.destination_id = destination_id
        self.create_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, str, str]] = []

    def create_playlist(
        self, name: str, *, public: bool = True, description: str = ""
    ) -> dict[str, Any]:
        assert public is True
        self.create_calls.append((name, description))
        return {"id": self.destination_id}

    def current_user(self) -> dict[str, Any]:
        return {"id": "owner"}

    def playlist_details(self, playlist_id: str) -> dict[str, Any]:
        return {"id": playlist_id, "name": "Noticias en Español", "owner": {"id": "owner"}}

    def change_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        self.update_calls.append((playlist_id, name, description))
        return {}


class _FailingUpdateSpotifyClient(_FakeSpotifyClient):
    def change_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        self.update_calls.append((playlist_id, name, description))
        raise SpotifyTransportError("simulated transport failure")


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
        self.status = OperationalStatus(configured=False)

    def reconcile(self, *, configured: bool) -> None:
        self.reconcile_calls.append(configured)

    def wake(self) -> None:
        self.wake_calls += 1


class _HandlerHarness(HandlerCaptureMixin, OperationalHealthHandler):
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
        self.engine_lifecycle = lifecycle  # type: ignore[assignment]
        self.auth_synchronization = None
        self.configuration_synchronization = ConfigurationSynchronization()
        self.managed_admin_service = service
        self.managed_admin_auth = auth_provider
        initialize_handler_capture(self, path=path, form=form, password=password)


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


class _BlockingCycleRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def run_cycle(self) -> EngineCycleResult:
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("test cycle was not released")
        return EngineCycleResult(
            started_at=NOW,
            finished_at=NOW,
            ok=True,
            sources=(),
            playlists=(),
        )


def _service(tmp_path: Path) -> tuple[ManagedAdminService, _Factory]:
    client = _FakeSpotifyClient()
    factory = _Factory(client)
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=factory,
    )
    return service, factory


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
    response = _response(unauthorized)
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/login"

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
    assert "Noticias en Español" in body
    assert "Noticias Internacional · ES" in body
    assert "International News · EN" in body
    assert "Cadena SER" in body
    assert "Radio Nacional de España" in body
    assert "Onda Cero" in body
    assert "ABC — Las Noticias de ABC" in body
    assert "CNN 5 Cosas" in body
    assert "/admin/covers/spain_spanish_news.jpg" in body


@pytest.mark.parametrize(
    ("notice_key", "message"),
    [
        ("playlist-activated", "Playlist activated successfully."),
        ("playlist-settings-saved", "Playlist settings saved successfully."),
        ("spotify-sync-applied", "Spotify metadata and cover applied successfully."),
        ("playlist-stopped", "Playlist is no longer managed."),
        ("playlist-provisioning-adopted", "Existing Spotify playlist adopted successfully."),
        (
            "playlist-provisioning-cleared",
            "Uncertain playlist creation state cleared successfully.",
        ),
    ],
)
def test_admin_success_notices_are_allow_listed_and_rendered(
    tmp_path: Path, notice_key: str, message: str
) -> None:
    service, _ = _service(tmp_path)
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=_FakeLifecycle(),
        path=f"/admin/?notice={notice_key}",
    )

    handler.do_GET()

    response = _response(handler)
    assert response.status == HTTPStatus.OK
    assert message in response.payload.decode()


def test_admin_notice_query_does_not_echo_unknown_values(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    sentinel = "notice-user-controlled-sentinel"
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=_FakeLifecycle(),
        path=f"/admin/?notice={sentinel}",
    )

    handler.do_GET()

    response = _response(handler)
    assert response.status == HTTPStatus.OK
    assert sentinel not in response.payload.decode()


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
    assert response.extra_headers["Location"] == "/admin/?notice=playlist-activated"
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
    assert "Location" not in _response(replay).extra_headers
    assert provider.calls == 1
    assert len(factory.client.create_calls) == 1


def test_clear_uncertain_provisioning_route_is_csrf_protected_and_local_only(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    journal = ProvisioningJournal(tmp_path / "managed-playlist-provisioning.json")
    journal.save(
        ProvisioningIntent(
            state=ProvisioningState.REQUEST_STARTED,
            template_id=template.id,
            display_name=template.display_name,
            description=template.description,
            cover_id=template.cover_id,
            source_ids=template.default_source_ids,
            retention_hours=template.retention_hours,
            max_episodes=template.max_episodes,
            max_duration_seconds=1800,
        )
    )
    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/provisioning/clear",
        form={"csrf_token": [security.issue_csrf_token()]},
        security=security,
    )
    handler.do_POST()

    response = _response(handler)
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/?notice=playlist-provisioning-cleared"
    assert journal.load() is None
    assert factory.client.create_calls == []
    assert lifecycle.reconcile_calls == [False]


def test_uncertain_provisioning_renders_only_recovery_actions(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    ProvisioningJournal(tmp_path / "managed-playlist-provisioning.json").save(
        ProvisioningIntent(
            state=ProvisioningState.REQUEST_STARTED,
            template_id=template.id,
            display_name=template.display_name,
            description=template.description,
            cover_id=template.cover_id,
            source_ids=template.default_source_ids,
            retention_hours=template.retention_hours,
            max_episodes=template.max_episodes,
            max_duration_seconds=1800,
        )
    )
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=_FakeLifecycle(),
        path="/admin/",
    )
    handler.do_GET()

    body = _response(handler).payload.decode()
    assert "Playlist creation needs recovery" in body
    assert "/admin/provisioning/adopt" in body
    assert "/admin/provisioning/clear" in body
    assert "Create Spotify playlist" not in body


def test_adopt_uncertain_provisioning_route_verifies_owner_and_starts_scheduler(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    ProvisioningJournal(tmp_path / "managed-playlist-provisioning.json").save(
        ProvisioningIntent(
            state=ProvisioningState.REQUEST_STARTED,
            template_id=template.id,
            display_name=template.display_name,
            description=template.description,
            cover_id=template.cover_id,
            source_ids=template.default_source_ids,
            retention_hours=template.retention_hours,
            max_episodes=template.max_episodes,
            max_duration_seconds=1800,
        )
    )
    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/provisioning/adopt",
        form={
            "csrf_token": [security.issue_csrf_token()],
            "destination_id": ["A" * 22],
        },
        auth_provider=_AccessTokenProvider(),
        security=security,
    )
    handler.do_POST()

    response = _response(handler)
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/?notice=playlist-provisioning-adopted"
    assert service.snapshot().managed[0].destination.external_id == "A" * 22
    assert factory.client.create_calls == []
    assert lifecycle.reconcile_calls == [True]


def test_activation_route_persists_requested_duration_without_metadata_io(tmp_path: Path) -> None:
    service, factory = _service(tmp_path)
    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    form = _activation_form(security.issue_csrf_token())
    form["max_duration_minutes"] = ["15"]
    form["max_duration_seconds_remainder"] = ["0"]
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/activate",
        form=form,
        auth_provider=_AccessTokenProvider(),
        security=security,
    )
    handler.do_POST()
    managed = service.snapshot().managed[0]
    response = _response(handler)
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/?notice=playlist-activated"
    assert managed.max_duration_seconds == 900
    assert factory.client.update_calls == []
    assert (
        compile_engine_config(service.catalog, service.store.load())
        .playlists[0]
        .duration_policy.default_max_seconds
        == 900
    )


def test_update_route_persists_exact_duration_without_metadata_io(tmp_path: Path) -> None:
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
            "display_name": [current.display_name],
            "description": [current.description],
            "cover_id": [current.cover_id],
            "source_id": [str(source_id) for source_id in current.source_ids],
            "enabled": ["1"],
            "max_duration_minutes": ["12"],
            "max_duration_seconds_remainder": ["0"],
        },
        security=security,
    )
    handler.do_POST()
    response = _response(handler)
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/?notice=playlist-settings-saved"
    assert service.snapshot().managed[0].max_duration_seconds == 720
    assert factory.client.update_calls == []


@pytest.mark.parametrize("disabled", [False, True])
def test_playlist_save_preserves_visible_custom_source(tmp_path: Path, disabled: bool) -> None:
    service, factory = _service(tmp_path)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    custom_show_id = "A" * 22
    custom = ManagedSource(
        id=SourceId(f"custom_{custom_show_id}"),
        spotify_show_id=custom_show_id,
        display_name="Installation source",
        origin=SourceOrigin.country(CountryCode("ES")),
        language=LanguageTag("es-419"),
        editorial_scope=EditorialScope.INTERNATIONAL,
        timezone="Europe/Madrid",
        parser_id=ParserId("release_date_title"),
        collection_filter_id=None,
        enabled=not disabled,
    )
    playlist = replace(
        activate_template(template, "destination"),
        source_ids=(SourceId("ser"), custom.id),
    )
    service.store.save(ManagedState(sources=(custom,), playlists=(playlist,)))
    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    get_handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/",
        security=security,
    )
    get_handler.do_GET()
    response = _response(get_handler)
    assert response.status == HTTPStatus.OK
    page = response.payload.decode()
    form_html = page.split('<form method="post" action="/admin/playlists/update">', 1)[1].split(
        "</form>", 1
    )[0]
    checked = re.findall(
        r'<input id="[^"]+" type="checkbox" name="source_id" value="([^"]+)" checked>',
        form_html,
    )
    retained = re.findall(r'<input type="hidden" name="source_id" value="([^"]+)">', form_html)
    assert checked == ["ser", str(custom.id)]
    assert retained == []
    assert f'type="checkbox" name="source_id" value="{custom.id}" checked' in page
    if disabled:
        assert "Inactive assignments" in page
        assert "Disabled sources contribute no episodes" in page
    assert '<input type="hidden" name="source_id" value="ser">' not in form_html
    csrf_token = re.search(r'<input type="hidden" name="csrf_token" value="([^"]+)">', form_html)
    assert csrf_token is not None

    post_handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/update",
        form={
            "csrf_token": [csrf_token.group(1)],
            "playlist_id": [str(playlist.id)],
            "display_name": ["Renamed playlist"],
            "description": [playlist.description],
            "cover_id": [playlist.cover_id],
            "source_id": checked + retained,
            "enabled": ["1"],
            "max_duration_minutes": ["12"],
            "max_duration_seconds_remainder": ["0"],
        },
        security=security,
    )
    post_handler.do_POST()
    post_response = _response(post_handler)
    assert post_response.status == HTTPStatus.SEE_OTHER
    assert post_response.extra_headers["Location"] == "/admin/?notice=playlist-settings-saved"
    persisted = service.store.load()
    assert persisted.sources == (custom,)
    assert persisted.playlists[0].source_ids == (SourceId("ser"), custom.id)
    assert persisted.playlists[0].display_name == "Renamed playlist"
    assert persisted.playlists[0].max_duration_seconds == 720
    assert factory.tokens == []
    assert factory.client.update_calls == []

    if disabled:
        config = compile_engine_config(BUILTIN_CATALOG, persisted)
        assert tuple(source.id for source in _required_sources(config)) == (SourceId("ser"),)
        runner = object.__new__(EngineRunner)
        runner.config = config

        class ReadyBuiltInOnly:
            def get_source_state(self, source_id: SourceId) -> SimpleNamespace:
                assert source_id == SourceId("ser")
                ser = next(source for source in config.sources if source.id == source_id)
                return SimpleNamespace(
                    spotify_show_id=ser.external_references[0].external_id,
                    coverage_proven=True,
                    coverage_cutoff_at=NOW - timedelta(hours=48),
                )

        runner.first_journal = ReadyBuiltInOnly()  # type: ignore[assignment]
        assert runner._playlist_sources_ready(config.playlists[0], now=NOW)


@pytest.mark.parametrize(
    ("minutes", "expected_status"),
    [
        ("0", HTTPStatus.CONFLICT),
        ("-1", HTTPStatus.BAD_REQUEST),
        ("bad", HTTPStatus.BAD_REQUEST),
        ("1441", HTTPStatus.CONFLICT),
    ],
)
def test_duration_route_rejects_invalid_values_without_state_change(
    tmp_path: Path, minutes: str, expected_status: HTTPStatus
) -> None:
    service, _ = _service(tmp_path)
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
            "display_name": [current.display_name],
            "description": [current.description],
            "cover_id": [current.cover_id],
            "source_id": [str(source_id) for source_id in current.source_ids],
            "enabled": ["1"],
            "max_duration_minutes": [minutes],
        },
        security=security,
    )
    handler.do_POST()
    response = _response(handler)
    assert response.status == expected_status
    assert "Location" not in response.extra_headers
    assert service.snapshot().managed[0] == current


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

    response = _response(handler)
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/?notice=playlist-settings-saved"
    assert service.snapshot().managed[0].source_ids == (SourceId("rne"),)
    assert factory.client.update_calls == []
    assert lifecycle.reconcile_calls == [True]


def test_metadata_edit_works_offline_and_does_not_write_spotify_metadata(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    _activate_direct(service)
    current = service.snapshot().managed[0]
    factory.client.update_calls.clear()
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
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/?notice=playlist-settings-saved"
    updated = service.snapshot().managed[0]
    assert updated.display_name == "Nuevo nombre"
    assert updated.description == current.description
    assert factory.client.update_calls == []
    assert lifecycle.reconcile_calls == [True]


def test_metadata_save_does_not_call_spotify_even_if_remote_metadata_would_fail(
    tmp_path: Path,
) -> None:
    client = _FailingUpdateSpotifyClient()
    factory = _Factory(client)
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=factory,
    )
    _activate_direct(service)
    client.update_calls.clear()
    current = service.snapshot().managed[0]
    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    provider = _AccessTokenProvider("update-token-sentinel")
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
        auth_provider=provider,
        security=security,
    )

    handler.do_POST()

    response = _response(handler)
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/?notice=playlist-settings-saved"
    updated = service.snapshot().managed[0]
    assert updated.display_name == "Nuevo nombre"
    assert client.update_calls == []
    assert provider.calls == 0
    assert lifecycle.reconcile_calls == [True]


def test_managed_state_error_is_500_and_does_not_expose_storage_path(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    _activate_direct(service)
    current = service.snapshot().managed[0]
    state_path = tmp_path / "managed-state.json"
    state_path.unlink()
    state_path.mkdir()
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
            "display_name": [current.display_name],
            "description": [current.description],
            "cover_id": [current.cover_id],
            "source_id": [str(source_id) for source_id in current.source_ids],
            "enabled": ["1"],
        },
        auth_provider=None,
        security=security,
    )

    handler.do_POST()

    response = _response(handler)
    assert response.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "Location" not in response.extra_headers
    body = response.payload.decode()
    assert "Managed playlist state could not be changed safely" in body
    assert str(state_path) not in body
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

    response = _response(handler)
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/?notice=playlist-stopped"
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

    for traversal_path in (
        "/admin/covers/%2e%2e%2fmanaged-state.json.jpg",
        "/admin/covers/..%2Fsecret.jpg",
    ):
        traversal = _HandlerHarness(
            tmp_path=tmp_path,
            service=service,
            lifecycle=lifecycle,
            path=traversal_path,
        )
        traversal.do_GET()
        assert _response(traversal).status == HTTPStatus.NOT_FOUND

    unauthorized = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/covers/spain_spanish_news.jpg",
        password=None,
    )
    unauthorized.do_GET()
    response = _response(unauthorized)
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/login"


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


def test_lifecycle_reactivation_waits_for_draining_cycle_then_restarts() -> None:
    runner = _BlockingCycleRunner()
    controller = EngineLifecycleController(  # type: ignore[arg-type]
        runner,
        interval=timedelta(hours=1),
    )

    controller.reconcile(configured=True)
    assert runner.started.wait(timeout=2)
    first_scheduler = controller.scheduler
    assert first_scheduler is not None

    controller.reconcile(configured=False)
    assert controller.scheduler is None
    assert controller.status.snapshot().configured is False

    runner.started.clear()
    controller.reconcile(configured=True)
    assert controller.status.snapshot().configured is True
    assert controller.scheduler is None
    assert not runner.started.wait(timeout=0.05)

    runner.release.set()
    _wait_until(
        lambda: controller.scheduler is not None and controller.scheduler is not first_scheduler
    )
    _wait_until(lambda: runner.calls >= 2)

    controller.shutdown()
    assert controller.scheduler is None
    assert controller.status.snapshot().configured is False


def test_managed_web_service_requires_spotify_auth(tmp_path: Path) -> None:
    assert _build_managed_admin_service(tmp_path, spotify_auth=None) is None
    assert (
        _build_managed_admin_service(
            tmp_path,
            spotify_auth=object(),  # type: ignore[arg-type]
        )
        is not None
    )


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

    response = _response(handler)
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/?notice=playlist-stopped"
    assert synchronization.depth == 0
    assert lifecycle.reconcile_calls == [False]


@pytest.mark.parametrize("handler_kind", ["engine", "diagnostics"])
@pytest.mark.parametrize("minutes,expected", [(20, 1234), (21, 1260)])
def test_range_update_handlers_preserve_seconds_and_wake_without_provider_io(
    tmp_path,
    handler_kind,
    minutes,
    expected,
):
    from news_bulletin_playlist.diagnostics_runtime import DiagnosticOperationalHealthHandler

    service, factory = _service(tmp_path)
    template = BUILTIN_CATALOG.playlists[0]
    current = replace(
        activate_template(template, "destination"),
        min_duration_seconds=1234,
        max_duration_seconds=1234,
    )
    service.store.save(ManagedState(playlists=(current,)))
    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/update",
        security=security,
        form={
            "csrf_token": [security.issue_csrf_token()],
            "playlist_id": [str(current.id)],
            "display_name": [current.display_name],
            "description": [current.description],
            "cover_id": [current.cover_id],
            "source_id": list(map(str, current.source_ids)),
            "enabled": ["1"],
            "min_duration_minutes": [str(minutes)],
            "max_duration_minutes": [str(minutes)],
            # Client seconds cannot override server-owned precision.
            "max_duration_seconds_remainder": ["59"],
        },
    )
    if handler_kind == "engine":
        handler.do_POST()
        assert _response(handler).status == HTTPStatus.SEE_OTHER
        assert lifecycle.reconcile_calls == [True]
    else:
        # Exercise the diagnostics handler's actual form plumbing without a network server.
        result = DiagnosticOperationalHealthHandler._update_managed_playlist(
            handler, service, handler._form
        )
        assert result == (str(current.id), True)
    updated = service.store.load().playlists[0]
    assert (updated.min_duration_seconds, updated.max_duration_seconds) == (expected, expected)
    assert factory.tokens == []
    assert factory.client.create_calls == []
    assert factory.client.update_calls == []


def test_minimum_only_change_blocks_stale_playlist_mutation(tmp_path):
    from news_bulletin_playlist.engine_runtime import ReloadingEngineCycleRunner
    from news_bulletin_playlist.reconciliation import SpotifyReconciliationError

    service, _ = _service(tmp_path)
    current = activate_template(BUILTIN_CATALOG.playlists[0], "destination")
    service.store.save(ManagedState(playlists=(current,)))
    expected = compile_engine_config(service.catalog, service.store.load())
    runner = object.__new__(ReloadingEngineCycleRunner)
    runner.data_dir = tmp_path
    runner.environ = {}
    runner.configuration_synchronization = ConfigurationSynchronization()
    service.store.save(ManagedState(playlists=(replace(current, min_duration_seconds=600),)))
    writes = []
    with (
        pytest.raises(SpotifyReconciliationError, match="configuration changed during cycle"),
        runner._playlist_mutation_guard(expected, expected.playlists[0]),
    ):
        writes.append("mutation")
    assert writes == []


def test_custom_template_routes_are_authenticated_local_only_and_csrf_protected(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    create_form = {
        "csrf_token": [security.issue_csrf_token()],
        "display_name": ["My local world updates"],
        "description": ["Independent news sources."],
        "cover_id": ["international_english_news"],
        "source_id": ["cnn", "un_news_en"],
        "retention_hours": ["48"],
        "max_episodes": ["100"],
        "min_duration_minutes": ["0"],
        "max_duration_minutes": ["30"],
        "language_tags": ["es-ES, en"],
        "country_codes": ["ES"],
    }
    create = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/templates/create",
        form=create_form,
        auth_provider=None,
        spotify_state=AuthorizationState.DISCONNECTED,
        security=security,
    )
    create.do_POST()
    assert _response(create).status == HTTPStatus.SEE_OTHER
    assert _response(create).extra_headers["Location"] == "/admin/?notice=local-draft-created"
    assert factory.tokens == []
    assert factory.client.create_calls == []
    assert factory.client.update_calls == []
    assert lifecycle.reconcile_calls == []
    assert lifecycle.wake_calls == 0

    draft = service.store.load().templates[0]
    assert service.store.load().schema_version == 7
    page = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/",
        spotify_state=AuthorizationState.DISCONNECTED,
        security=security,
    )
    page.do_GET()
    body = _response(page).payload.decode()
    assert "My playlists" in body
    assert "My local world updates" in body
    assert "Save local draft" in body
    assert "/admin/playlists/templates/create" in body
    assert "/admin/playlists/templates/update" in body
    assert "/admin/playlists/templates/delete" in body

    # Reusing the one-use CSRF token must not write or create a second draft.
    replay = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/templates/create",
        form=create_form,
        security=security,
    )
    replay.do_POST()
    assert _response(replay).status == HTTPStatus.FORBIDDEN
    assert len(service.store.load().templates) == 1

    update_form = dict(create_form)
    update_form["csrf_token"] = [security.issue_csrf_token()]
    update_form["template_id"] = [str(draft.id)]
    update_form["display_name"] = ["Renamed local draft"]
    update_form["hidden"] = ["1"]
    update = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/templates/update",
        form=update_form,
        auth_provider=None,
        security=security,
    )
    update.do_POST()
    assert _response(update).status == HTTPStatus.SEE_OTHER
    assert _response(update).extra_headers["Location"] == "/admin/?notice=local-draft-updated"
    assert service.store.load().templates[0].hidden is True
    assert service.store.load().templates[0].display_name == "Renamed local draft"

    delete = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/templates/delete",
        form={
            "csrf_token": [security.issue_csrf_token()],
            "template_id": [str(draft.id)],
        },
        auth_provider=None,
        security=security,
    )
    delete.do_POST()
    assert _response(delete).status == HTTPStatus.SEE_OTHER
    assert _response(delete).extra_headers["Location"] == "/admin/?notice=local-draft-deleted"
    assert service.store.load().templates == ()
    assert factory.tokens == []
    assert factory.client.create_calls == []
    assert lifecycle.reconcile_calls == []


def test_custom_draft_explicit_activation_is_separate_from_local_save(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    draft = service.create_local_template(
        display_name="My short news",
        description="Local-only until explicit publication",
        cover_id="international_spanish_news",
        source_ids=("cnn",),
        retention_hours=48,
        max_episodes=100,
        min_duration_minutes=0,
        max_duration_minutes=30,
    )
    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    disconnected = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/",
        security=security,
        spotify_state=AuthorizationState.DISCONNECTED,
    )
    disconnected.do_GET()
    assert "Create Spotify playlist from draft" not in _response(disconnected).payload.decode()

    connected = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/",
        security=security,
        spotify_state=AuthorizationState.CONNECTED,
    )
    connected.do_GET()
    assert "Create Spotify playlist from draft" in _response(connected).payload.decode()
    assert factory.tokens == []
    assert lifecycle.reconcile_calls == []

    auth = _AccessTokenProvider()
    form = {
        "csrf_token": [security.issue_csrf_token()],
        "template_id": [str(draft.id)],
        "display_name": [draft.display_name],
        "description": [draft.description],
        "cover_id": [draft.cover_id],
        "source_id": [str(source_id) for source_id in draft.default_source_ids],
        "min_duration_minutes": ["0"],
        "max_duration_minutes": ["30"],
    }
    publish = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/activate",
        form=form,
        security=security,
        auth_provider=auth,
    )
    publish.do_POST()
    assert _response(publish).status == HTTPStatus.SEE_OTHER
    assert _response(publish).extra_headers["Location"] == "/admin/?notice=playlist-activated"
    assert factory.tokens == ["access-token-sentinel"]
    assert len(factory.client.create_calls) == 1
    assert factory.client.update_calls == []
    assert len(service.store.load().playlists) == 1
    assert service.store.load().templates == (draft,)
    assert lifecycle.reconcile_calls == [True]

    already_active = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/",
        security=security,
    )
    already_active.do_GET()
    assert "Create Spotify playlist from draft" not in _response(already_active).payload.decode()


def test_quarantine_repair_admin_route_is_authenticated_csrf_bound_and_local_only(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    draft = service.create_local_template(
        display_name="Draft to recover",
        description="Local only",
        cover_id="international_spanish_news",
        source_ids=("cnn",),
        retention_hours=48,
        max_episodes=100,
        min_duration_minutes=0,
        max_duration_minutes=30,
    )
    raw = json.loads(service.store.path.read_bytes())
    raw["templates"][0]["max_duration_seconds"] = -100
    raw["templates"][0]["description"] = "PRIVATE-RAW-METADATA"
    service.store.path.write_text(json.dumps(raw), encoding="utf-8")
    quarantined = service.store.load().quarantined_templates[0]
    assert quarantined.template_id == str(draft.id)
    fingerprint = hashlib.sha256(quarantined.raw_json.encode("utf-8")).hexdigest()

    lifecycle = _FakeLifecycle()
    security = LanAdminSecurity(_PASSWORD)
    page = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/",
        security=security,
    )
    page.do_GET()
    html_body = _response(page).payload.decode()
    assert "Custom playlist drafts require repair" in html_body
    assert "/admin/playlists/templates/quarantine/discard" in html_body
    assert "PRIVATE-RAW-METADATA" not in html_body

    form = {
        "csrf_token": [security.issue_csrf_token()],
        "quarantine_index": ["0"],
        "quarantine_fingerprint": [fingerprint],
        "backup_confirmed": ["1"],
    }
    forbidden = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/templates/quarantine/discard",
        form=form,
        security=security,
        password=None,
    )
    forbidden.do_POST()
    assert _response(forbidden).status != HTTPStatus.SEE_OTHER or (
        _response(forbidden).extra_headers.get("Location") == "/admin/login"
    )
    assert len(service.store.load().quarantined_templates) == 1

    missing = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/templates/quarantine/discard",
        form={**form, "csrf_token": [security.issue_csrf_token()], "backup_confirmed": []},
        security=security,
    )
    missing.do_POST()
    assert _response(missing).status == HTTPStatus.BAD_REQUEST
    assert len(service.store.load().quarantined_templates) == 1

    # Give this independently authorized request a fresh one-use token.
    accepted_form = {**form, "csrf_token": [security.issue_csrf_token()]}
    accepted = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/templates/quarantine/discard",
        form=accepted_form,
        security=security,
    )
    accepted.do_POST()
    assert _response(accepted).status == HTTPStatus.SEE_OTHER
    assert _response(accepted).extra_headers["Location"] == (
        "/admin/?notice=local-quarantine-discarded"
    )
    assert service.store.load().quarantined_templates == ()
    assert factory.tokens == []
    assert factory.client.create_calls == []
    assert lifecycle.reconcile_calls == []

    replay = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=lifecycle,
        path="/admin/playlists/templates/quarantine/discard",
        form=accepted_form,
        security=security,
    )
    replay.do_POST()
    assert _response(replay).status == HTTPStatus.FORBIDDEN


def test_custom_playlist_creation_with_cover_failure_shows_pending_notice(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    # A real service always has a cover loader; this stub lacks upload support.
    service.cover_loader = lambda _cover_id: b"\xff\\xd8fixture\xff\\xd9"
    draft = service.create_local_template(
        display_name="Personalizada",
        description="Boletines de actualidad",
        cover_id="international_spanish_news",
        source_ids=("cnn",),
        retention_hours=48,
        max_episodes=100,
        min_duration_minutes=0,
        max_duration_minutes=30,
    )
    security = LanAdminSecurity(_PASSWORD)
    handler = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=_FakeLifecycle(),
        path="/admin/playlists/activate",
        security=security,
        auth_provider=_AccessTokenProvider(),
        form={
            "csrf_token": [security.issue_csrf_token()],
            "template_id": [str(draft.id)],
            "display_name": [draft.display_name],
            "description": [draft.description],
            "cover_id": [draft.cover_id],
            "source_id": ["cnn"],
            "min_duration_minutes": ["0"],
            "max_duration_minutes": ["30"],
        },
    )
    handler.do_POST()
    result = _response(handler)
    assert result.status == HTTPStatus.SEE_OTHER
    assert result.extra_headers["Location"] == "/admin/?notice=playlist-activated-cover-pending"
    assert len(factory.client.create_calls) == 1
    assert len(service.store.load().playlists) == 1
    assert service.provisioning_journal.load() is None
    assert service.last_initial_cover_error == "local cover unavailable"
