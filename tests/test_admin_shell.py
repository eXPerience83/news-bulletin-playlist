from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any

from _html import assert_valid_admin_html
from _http_handler import HandlerCaptureMixin, captured_response, initialize_handler_capture

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.diagnostics_runtime import DiagnosticOperationalHealthHandler
from news_bulletin_playlist.engine import (
    EngineCycleResult,
    OperationalStatus,
    PlaylistCycleOutcome,
    SourceCycleOutcome,
)
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.lan_admin import LanAdminSecurity
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_admin_web import render_managed_admin_page
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.spotify.auth import AuthorizationState

_PASSWORD = "long-enough-admin-password"
_SECRET = "raw-provider-error-sentinel"
_NOW = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


class _SpotifyState:
    def authorization_state(self) -> AuthorizationState:
        return AuthorizationState.CONNECTED


class _FakeSpotifyClient:
    def create_playlist(
        self,
        name: str,
        *,
        public: bool = True,
        description: str = "",
    ) -> dict[str, Any]:
        del name, description
        assert public is True
        return {"id": "admin-shell-destination"}


class _Factory:
    def __call__(self, access_token: str) -> _FakeSpotifyClient:
        del access_token
        return _FakeSpotifyClient()


class _Handler(HandlerCaptureMixin, DiagnosticOperationalHealthHandler):
    def __init__(
        self,
        *,
        tmp_path: Path,
        path: str,
        service: ManagedAdminService | None,
        password: str | None = _PASSWORD,
        status: OperationalStatus | None = None,
    ) -> None:
        self.data_dir = tmp_path
        self.admin_security = LanAdminSecurity(_PASSWORD)
        self.spotify_auth = _SpotifyState()  # type: ignore[assignment]
        self.operational_status = status or OperationalStatus(configured=False)
        self.engine_lifecycle = None
        self.auth_synchronization = None
        self.configuration_synchronization = ConfigurationSynchronization()
        self.managed_admin_service = service
        self.managed_admin_auth = None
        store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite3")
        store.initialize()
        self.diagnostic_store = store
        self.spotify_request_metrics = None
        self.operational_diagnostics = None
        initialize_handler_capture(self, path=path, password=password)


def _service(tmp_path: Path) -> ManagedAdminService:
    return ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=_Factory(),
    )


def _get(
    tmp_path: Path,
    *,
    path: str,
    service: ManagedAdminService | None,
    password: str | None = _PASSWORD,
    status: OperationalStatus | None = None,
) -> tuple[HTTPStatus, str]:
    handler = _Handler(
        tmp_path=tmp_path,
        path=path,
        service=service,
        password=password,
        status=status,
    )
    handler.do_GET()
    response = captured_response(handler)
    return HTTPStatus(response.status), response.payload.decode("utf-8")


def test_admin_routes_require_auth_and_share_navigation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    paths = ("/admin/", "/admin/sources", "/admin/diagnostics")

    for path in paths:
        status, _body = _get(tmp_path, path=path, service=service, password=None)
        assert status is HTTPStatus.SEE_OTHER

    pages = {path: _get(tmp_path, path=path, service=service)[1] for path in paths}
    expected_active = {
        "/admin/": "/admin/",
        "/admin/sources": "/admin/sources",
        "/admin/diagnostics": "/admin/diagnostics",
    }
    for path, body in pages.items():
        assert_valid_admin_html(body)
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in body
        assert '<a class="admin-tab" href="/admin/">Playlists</a>' in body or (path == "/admin/")
        assert 'href="/admin/sources"' in body
        assert 'href="/admin/diagnostics"' in body
        assert "Copyright (c) 2026 Roberto Ribes" in body
        assert '<a class="button" href="/">Status</a>' in body
        assert 'action="/admin/logout"' in body
        assert body.count('aria-current="page"') == 1
        active = expected_active[path]
        assert f'class="admin-tab is-active" href="{active}" aria-current="page"' in body


def test_playlists_keep_source_controls_and_sources_move_to_read_only_page(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    playlists_status, playlists = _get(tmp_path, path="/admin/", service=service)
    sources_status, sources = _get(tmp_path, path="/admin/sources", service=service)

    assert playlists_status is HTTPStatus.OK
    assert sources_status is HTTPStatus.OK
    assert 'id="source-catalog-heading"' not in playlists
    assert 'name="source_id"' in playlists
    control_ids = re.findall(
        r'<input id="([^"]+)" type="checkbox" name="source_id"',
        playlists,
    )
    assert control_ids
    assert len(control_ids) == len(set(control_ids))

    assert 'id="source-catalog-heading"' in sources
    assert '<div class="card table-card"><div class="table-scroll">' in sources
    assert '<th scope="col">Origin</th>' in sources
    assert '<th scope="col">Ownership</th>' in sources
    assert "Built-in" in sources
    for source_name in (
        "Cadena SER",
        "Radio Nacional de España",
        "Onda Cero",
        "ABC — Las Noticias de ABC",
        "CNN 5 Cosas",
    ):
        assert source_name in sources
    assert 'action="/admin/logout"' in sources
    assert 'type="checkbox" name="source_id"' not in sources
    assert 'action="/admin/sources/audit"' in sources
    assert 'href="/admin/sources/new">Add source</a>' in sources
    assert "Remove source" not in sources


def test_un_origin_is_rendered_in_sources_and_playlist_selectors(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _, sources = _get(tmp_path, path="/admin/sources", service=service)
    _, playlists = _get(tmp_path, path="/admin/", service=service)

    assert re.search(
        r"United Nations — UN News Today.*?</td><td>UN</td><td><code>INT</code></td><td>en",
        sources,
    )
    assert re.search(
        r"United Nations — ONU en minutos.*?</td><td>UN</td><td><code>INT</code></td><td>es",
        sources,
    )
    assert "United Nations — UN News Today (UN · INT · en)" in playlists
    assert "United Nations — ONU en minutos (UN · INT · es)" in playlists
    assert "United Nations — UN News Today (US ·" not in playlists
    assert "United Nations — ONU en minutos (US ·" not in playlists


def test_sources_are_readable_without_oauth_after_authentication(tmp_path: Path) -> None:
    unauthorized, _ = _get(
        tmp_path,
        path="/admin/sources",
        service=None,
        password=None,
    )
    authenticated, body = _get(
        tmp_path,
        path="/admin/sources",
        service=None,
    )

    assert unauthorized is HTTPStatus.SEE_OTHER
    assert authenticated is HTTPStatus.OK
    assert "Broadcasters and sources" in body
    assert "Available source examples" in body
    assert "Import sources" in body
    assert 'action="/admin/sources/audit"' not in body


def test_diagnostics_shell_preserves_filters_metrics_and_mobile_table_wrapper(
    tmp_path: Path,
) -> None:
    status, body = _get(
        tmp_path,
        path="/admin/diagnostics?severity=warning&source=rne&hours=168&limit=50",
        service=_service(tmp_path),
    )

    assert status is HTTPStatus.OK
    assert 'action="/admin/diagnostics"' in body
    assert '<option value="WARNING" selected>WARNING</option>' in body
    assert 'name="source" value="rne"' in body
    assert '<option value="168" selected>7 days</option>' in body
    assert 'name="limit" min="1" max="500"' in body
    assert "/admin/diagnostics/export.zip?" in body
    assert 'id="spotify-request-metrics-heading"' in body
    assert "Spotify request metrics are unavailable." in body
    assert '<div class="card table-card"><div class="table-scroll">' in body
    assert '<th scope="col">Details</th>' in body


def test_admin_shell_escapes_operator_messages(tmp_path: Path) -> None:
    service = _service(tmp_path)
    sentinel = '<script>alert("admin")</script> & "quoted"'
    body = render_managed_admin_page(
        snapshot=service.snapshot(),
        state=service.store.load(),
        catalog=BUILTIN_CATALOG,
        spotify_state=AuthorizationState.CONNECTED,
        csrf_token='csrf-<&"token',
        last_cycle=None,
        lan_mode=False,
        notice=sentinel,
        error=sentinel,
    ).decode()

    assert_valid_admin_html(body)
    assert sentinel not in body
    assert "&lt;script&gt;alert(&quot;" in body
    assert 'value="csrf-&lt;&amp;&quot;token"' in body


def test_admin_html_never_echoes_raw_cycle_errors(tmp_path: Path) -> None:
    service = _service(tmp_path)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="setup-token",
    )
    cycle = EngineCycleResult(
        started_at=_NOW,
        finished_at=_NOW + timedelta(seconds=1),
        ok=False,
        sources=(
            SourceCycleOutcome(
                source_id=SourceId("rne"),
                ok=False,
                episode_count=0,
                refreshed=False,
                refresh_reason="change",
                api_request_count=0,
                detector_ok=False,
                detector_changed=True,
                last_detector_success_at=None,
                last_catalogue_refresh_at=None,
                error=_SECRET,
            ),
        ),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=managed.id,
                ok=False,
                desired_count=0,
                applied_count=None,
                wrote=None,
                last_success_at=None,
                error=_SECRET,
            ),
        ),
        error=_SECRET,
    )
    operational = OperationalStatus(configured=True)
    operational.finish_cycle(cycle, next_run_at=_NOW + timedelta(minutes=10))

    _, playlists = _get(
        tmp_path,
        path="/admin/",
        service=service,
        status=operational,
    )
    _, sources = _get(
        tmp_path,
        path="/admin/sources",
        service=service,
        status=operational,
    )

    assert _SECRET not in playlists
    assert _SECRET not in sources
    assert "Failed — see Diagnostics" in playlists
    assert ">Failed<" in sources
