"""Isolated Source Admin HTTP test server and synthetic provider fixtures."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path
from typing import Any

from _admin_http import csrf as _csrf
from _admin_http import request as _request

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.diagnostics_runtime import DiagnosticOperationalHealthHandler
from news_bulletin_playlist.engine import OperationalStatus
from news_bulletin_playlist.engine_cycle_synchronization import EngineCycleSynchronization
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.lan_admin import LanAdminSecurity
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_state import (
    ManagedSource,
    ManagedState,
    ManagedStateStore,
    activate_template,
)
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.source_admin import SourceAdminService
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetectorJournal,
    PublicEpisodeObservation,
    PublicShowDetectorError,
)
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal

SHOW = "A" * 22
OTHER = "B" * 22
PASSWORD = "long-enough-admin-password"


class _Auth:
    def __init__(self) -> None:
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        self.calls += 1
        return "secret-access-token"


class _Client:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.name = '"><script>provider-sentinel</script>'
        self.on_episodes: Callable[[], None] | None = None
        self.two_episodes = False
        # Audit test cases may supply the publisher's local catalogue day.
        # Without this, a run near Madrid midnight gives inconsistent dates.
        self.release_day_override: str | None = None

    def show_details(self, show_id: str) -> dict[str, Any]:
        self.calls.append("details")
        if self.error is not None:
            raise self.error
        return {
            "id": show_id,
            "type": "show",
            "uri": f"spotify:show:{show_id}",
            "name": self.name,
            "publisher": "<img src=x onerror=alert(1)>",
            "languages": ["es"],
            "total_episodes": 1,
            "secret": "secret-provider-body",
        }

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.calls.append("episodes")
        if self.on_episodes is not None:
            callback = self.on_episodes
            self.on_episodes = None
            callback()
        release_day = self.release_day_override or datetime.now(UTC).date().isoformat()
        items = [
            {
                "id": OTHER,
                "uri": f"spotify:episode:{OTHER}",
                "name": "<img src=x onerror=alert(1)>",
                "release_date": release_day,
                "release_date_precision": "day",
                "duration_ms": None,
                "secret": "secret-refresh-token",
            }
        ]
        if self.two_episodes:
            items.append(
                {
                    "id": "C" * 22,
                    "uri": f"spotify:episode:{'C' * 22}",
                    "name": "Another bulletin",
                    "release_date": release_day,
                    "release_date_precision": "day",
                    "duration_ms": 902_000,
                }
            )
        return {
            "items": items,
            "total": len(items),
            "next": None,
            "limit": limit,
            "offset": offset,
        }


class _PublicAudit:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def observe_episode(
        self, source_id: SourceId, spotify_show_id: str, *, observed_at: datetime
    ) -> PublicEpisodeObservation:
        self.calls += 1
        if self.fail:
            raise PublicShowDetectorError("secret-public-body")
        return PublicEpisodeObservation(
            source_id=source_id,
            spotify_show_id=spotify_show_id,
            episode_id=OTHER,
            episode_uri=f"spotify:episode:{OTHER}",
            name="Public bulletin",
            title="Public bulletin",
            release_date_raw=observed_at.date().isoformat(),
            duration_ms=305_000,
            related_entity_uri=f"spotify:show:{spotify_show_id}",
            observed_at=observed_at,
        )


class _Handler(DiagnosticOperationalHealthHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _Lifecycle:
    def __init__(self) -> None:
        self.reconcile_calls: list[bool] = []

    def reconcile(self, *, configured: bool) -> None:
        self.reconcile_calls.append(configured)


@contextmanager
def _server(
    tmp_path: Path, *, lifecycle: _Lifecycle | None = None
) -> Iterator[tuple[HTTPServer, _Client, ManagedStateStore]]:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    synchronization = ConfigurationSynchronization()
    auth = _Auth()
    client = _Client()

    class _ScopedHandler(_Handler):
        """Give each HTTP server its own mutable handler configuration."""

    _ScopedHandler.admin_security = LanAdminSecurity(PASSWORD)
    _ScopedHandler.data_dir = tmp_path
    _ScopedHandler.spotify_auth = None
    _ScopedHandler.operational_status = OperationalStatus(configured=False)
    _ScopedHandler.engine_lifecycle = lifecycle or _Lifecycle()  # type: ignore[assignment]
    _ScopedHandler.auth_synchronization = None
    _ScopedHandler.configuration_synchronization = synchronization
    _ScopedHandler.managed_admin_service = ManagedAdminService(store)
    _ScopedHandler.managed_admin_auth = None
    diagnostic_store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite3")
    diagnostic_store.initialize()
    _ScopedHandler.diagnostic_store = diagnostic_store
    _ScopedHandler.operational_diagnostics = None
    runtime_store = SQLiteStore(tmp_path / "state.sqlite3")
    runtime_store.initialize()
    first_journal = SpotifyFirstJournal(runtime_store)
    first_journal.initialize()
    detector_journal = PublicDetectorJournal(runtime_store)
    detector_journal.initialize()
    cycle_synchronization = EngineCycleSynchronization()
    _ScopedHandler.source_admin_service = SourceAdminService(
        store,
        synchronization,
        auth,
        SpotifyRateLimitJournal(runtime_store),
        None,
        client_factory=lambda token: client,
        first_journal=first_journal,
        detector_journal=detector_journal,
        cycle_synchronization=cycle_synchronization,
        provisioning_journal=_ScopedHandler.managed_admin_service.provisioning_journal,
        public_client=_PublicAudit(),
    )
    server = HTTPServer(("127.0.0.1", 0), _ScopedHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield server, client, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _inspect(server: HTTPServer, cookie: str, locator: str = SHOW) -> tuple[int, bytes]:
    status, _, page = _request(server, "GET", "/admin/sources/new", cookie=cookie)
    assert status == HTTPStatus.OK
    status, _, review = _request(
        server,
        "POST",
        "/admin/sources/inspect",
        cookie=cookie,
        form={"csrf_token": _csrf(page), "locator": locator},
    )
    return status, review


def _audit(server: HTTPServer, cookie: str, source_id: str) -> tuple[int, bytes]:
    status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
    assert status == HTTPStatus.OK
    status, _, review = _request(
        server,
        "POST",
        "/admin/sources/audit",
        cookie=cookie,
        form={"csrf_token": _csrf(page), "source_id": source_id},
    )
    return status, review


def _create_form(review: bytes) -> dict[str, str]:
    return {
        "csrf_token": _csrf(review),
        "spotify_show_id": SHOW,
        "display_name": "Operator news",
        "origin_kind": "institution",
        "origin_code": "UN",
        "language": "es-419",
        "editorial_scope": "INT",
        "timezone": "Europe/Madrid",
        "parser_id": "release_date_title",
        "collection_filter_id": "",
    }


def _configured_source(
    server: HTTPServer, store: ManagedStateStore, *, enabled: bool = True
) -> ManagedSource:
    service = server.RequestHandlerClass.source_admin_service
    assert service is not None
    source = service.validate_new_source(
        show_id=SHOW,
        display_name="Original news",
        origin_kind="country",
        origin_code="ES",
        language="es",
        editorial_scope="NAT",
        timezone="Europe/Madrid",
        parser_id="release_date_title",
        collection_filter_id=None,
    )
    source = replace(source, enabled=enabled)
    playlist = replace(
        activate_template(BUILTIN_CATALOG.playlist("spain_spanish_news"), "destination"),
        source_ids=(source.id,),
    )
    store.save(ManagedState(sources=(source,), playlists=(playlist,)))
    return source


def _update_form(token: str) -> dict[str, str]:
    return {
        "csrf_token": token,
        "source_id": f"custom_{SHOW}",
        "display_name": "  Revised news  ",
        "origin_kind": "institution",
        "origin_code": "UN",
        "language": "es-419",
        "editorial_scope": "INT",
    }
