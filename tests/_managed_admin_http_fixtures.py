"""In-memory managed Admin handler harness with per-instance auth and lifecycle."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from _http_handler import HandlerCaptureMixin, initialize_handler_capture

from news_bulletin_playlist.engine import OperationalStatus
from news_bulletin_playlist.engine_runtime import (
    ConfigurationSynchronization,
    OperationalHealthHandler,
)
from news_bulletin_playlist.lan_admin import LanAdminSecurity
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.spotify.auth import AuthorizationState

_PASSWORD = "long-enough-admin-password"


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
