from __future__ import annotations

import html
import os
import signal
import threading
import urllib.parse
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path
from types import FrameType

from news_bulletin_playlist import __version__
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics import DiagnosticEventStore, DiagnosticSeverity
from news_bulletin_playlist.effective_config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_FILENAME,
    load_effective_config,
)
from news_bulletin_playlist.engine import (
    DEFAULT_ENGINE_INTERVAL,
    EngineCycleResult,
    EngineCycleRunner,
    EngineRunner,
    EngineScheduler,
    OperationalStatus,
    OperationalStatusSnapshot,
    SpotifyAuthProvider,
)
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.lan_admin import LanAdminHandler, build_engine_runtime_auth
from news_bulletin_playlist.managed_admin import (
    ManagedAdminError,
    ManagedAdminService,
    SpotifyPlaylistSyncError,
)
from news_bulletin_playlist.managed_admin_web import (
    playlist_id_from_form,
    render_managed_admin_page,
    single_form_value,
)
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedStateError,
    ManagedStateStore,
)
from news_bulletin_playlist.models import EngineConfig
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.runtime import (
    DEFAULT_DATA_DIR,
    DEFAULT_HEALTH_HOST,
    DEFAULT_HEALTH_PORT,
    _data_dir_ready,
    initialize_runtime_storage,
)
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify.auth import (
    AuthorizationState,
    SpotifyAuthError,
    SpotifyAuthService,
)
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError

_INTERVAL_SECONDS_ENV = "NEWS_PLAYLIST_INTERVAL_SECONDS"
_MIN_RUNTIME_INTERVAL = timedelta(minutes=1)
_SCHEDULER_SHUTDOWN_WAIT_SECONDS = 10.0
_BUNDLED_COVER_DIR = Path("/opt/news-bulletin-playlist/covers")
_MANAGED_POST_PATHS = {
    "/admin/playlists/activate",
    "/admin/playlists/update",
    "/admin/playlists/sync",
    "/admin/playlists/stop",
}
_ADMIN_NOTICE_MESSAGES = {
    "spotify-sync-applied": "Spotify metadata and cover applied successfully.",
}


class AuthSynchronization:
    """Serialize token refresh and Web UI authorization against one credential store."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._lock:
            yield


class ConfigurationSynchronization:
    """Serialize durable configuration mutations against complete engine cycles."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._lock:
            yield


class _LockedAuthProvider:
    def __init__(self, service: SpotifyAuthService, synchronization: AuthSynchronization) -> None:
        self.service = service
        self.synchronization = synchronization

    def get_access_token(self, *, now: datetime | None = None) -> str:
        with self.synchronization.hold():
            return self.service.get_access_token(now=now)


class _MutableOperationalStatus(OperationalStatus):
    """Keep historical cycle data while the managed scheduler switches on or off."""

    def set_configured(self, configured: bool) -> None:
        with self._lock:
            self._configured = configured
            if not configured:
                self._running = False
                self._next_run_at = None


class EngineLifecycleController:
    """Start, wake and drain one scheduler as active managed playlists change."""

    def __init__(
        self,
        runner: EngineCycleRunner,
        *,
        interval: timedelta,
        diagnostics: OperationalDiagnostics | None = None,
    ) -> None:
        self.runner = runner
        self.interval = interval
        self.diagnostics = diagnostics
        self.status = _MutableOperationalStatus(configured=False)
        self._scheduler: EngineScheduler | None = None
        self._scheduler_stop: threading.Event | None = None
        self._scheduler_thread: threading.Thread | None = None
        self._draining_threads: list[threading.Thread] = []
        self._restart_waiter: threading.Thread | None = None
        self._lock = threading.RLock()

    @property
    def scheduler(self) -> EngineScheduler | None:
        with self._lock:
            return self._scheduler

    def reconcile(self, *, configured: bool) -> None:
        """Make scheduler existence match whether any effective playlist is active."""
        with self._lock:
            self.status.set_configured(configured)
            self._prune_draining_locked()
            if configured:
                if self._scheduler is not None:
                    self._wake_scheduler_locked()
                elif self._draining_threads:
                    self._ensure_restart_waiter_locked()
                else:
                    self._start_scheduler_locked()
                return
            self._request_scheduler_stop_locked()

    def wake(self) -> None:
        with self._lock:
            if self._scheduler is not None:
                self._wake_scheduler_locked()

    def shutdown(self) -> None:
        with self._lock:
            self.status.set_configured(False)
            self._request_scheduler_stop_locked()
            draining = tuple(self._draining_threads)
        for scheduler_thread in draining:
            scheduler_thread.join(timeout=_SCHEDULER_SHUTDOWN_WAIT_SECONDS)
        still_running = [thread for thread in draining if thread.is_alive()]
        with self._lock:
            self._prune_draining_locked()
        if still_running:
            self._emit_scheduler(
                DiagnosticSeverity.WARNING,
                "scheduler_shutdown_timeout",
                details={"phase": "scheduler"},
            )

    def _start_scheduler_locked(self) -> None:
        scheduler = EngineScheduler(
            self.runner,
            self.status,
            interval=self.interval,
        )
        scheduler_stop = threading.Event()
        scheduler_thread = threading.Thread(
            target=scheduler.run,
            args=(scheduler_stop,),
            name="news-playlist-engine",
            daemon=True,
        )
        self._scheduler = scheduler
        self._scheduler_stop = scheduler_stop
        self._scheduler_thread = scheduler_thread
        scheduler_thread.start()
        self._emit_scheduler(
            DiagnosticSeverity.INFO,
            "scheduler_started",
            details={"next_state": "running"},
        )

    def _wake_scheduler_locked(self) -> None:
        scheduler = self._scheduler
        if scheduler is None:
            return
        scheduler.wake()
        self._emit_scheduler(
            DiagnosticSeverity.INFO,
            "scheduler_wake_requested",
            details={"next_state": "running"},
        )

    def _request_scheduler_stop_locked(self) -> None:
        scheduler = self._scheduler
        scheduler_stop = self._scheduler_stop
        scheduler_thread = self._scheduler_thread
        self._scheduler = None
        self._scheduler_stop = None
        self._scheduler_thread = None
        if scheduler is None or scheduler_stop is None or scheduler_thread is None:
            return
        scheduler_stop.set()
        scheduler.wake()
        self._emit_scheduler(
            DiagnosticSeverity.INFO,
            "scheduler_stop_requested",
            details={"next_state": "stopped"},
        )
        if scheduler_thread.is_alive() and scheduler_thread not in self._draining_threads:
            self._draining_threads.append(scheduler_thread)

    def _emit_scheduler(
        self,
        severity: DiagnosticSeverity,
        event_name: str,
        *,
        details: dict[str, str] | None = None,
    ) -> None:
        diagnostics = self.diagnostics
        if diagnostics is None:
            return
        diagnostics.emit(
            occurred_at=datetime.now(UTC),
            severity=severity,
            component="scheduler",
            event_name=event_name,
            details=details,
        )

    def _prune_draining_locked(self) -> None:
        self._draining_threads = [thread for thread in self._draining_threads if thread.is_alive()]

    def _ensure_restart_waiter_locked(self) -> None:
        if self._restart_waiter is not None and self._restart_waiter.is_alive():
            return
        restart_waiter = threading.Thread(
            target=self._restart_after_drain,
            name="news-playlist-engine-restart",
            daemon=True,
        )
        self._restart_waiter = restart_waiter
        restart_waiter.start()

    def _restart_after_drain(self) -> None:
        while True:
            with self._lock:
                self._prune_draining_locked()
                if not self._draining_threads:
                    self._restart_waiter = None
                    if self.status.snapshot().configured and self._scheduler is None:
                        self._start_scheduler_locked()
                    return
                draining = tuple(self._draining_threads)
            for scheduler_thread in draining:
                scheduler_thread.join(timeout=0.25)


class ReloadingEngineCycleRunner:
    """Load and validate durable configuration immediately before every engine cycle."""

    def __init__(
        self,
        data_dir: Path,
        environ: Mapping[str, str],
        store: SQLiteStore,
        auth: SpotifyAuthProvider,
        configuration_synchronization: ConfigurationSynchronization | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.environ = environ
        self.store = store
        self.auth = auth
        self.configuration_synchronization = configuration_synchronization

    def run_cycle(self) -> EngineCycleResult:
        synchronization = self.configuration_synchronization
        if synchronization is None:
            return self._run_cycle()
        with synchronization.hold():
            return self._run_cycle()

    def _run_cycle(self) -> EngineCycleResult:
        started_at = datetime.now(UTC)
        try:
            config = _load_runtime_config(self.data_dir, self.environ)
        except RuntimeError as exc:
            return EngineCycleResult(
                started_at=started_at,
                finished_at=datetime.now(UTC),
                ok=False,
                sources=(),
                playlists=(),
                error=str(exc),
            )
        if config is None:
            return EngineCycleResult(
                started_at=started_at,
                finished_at=datetime.now(UTC),
                ok=False,
                sources=(),
                playlists=(),
                error="production engine configuration is no longer available",
            )
        return EngineRunner(config, self.store, self.auth).run_cycle()


class OperationalHealthHandler(LanAdminHandler):
    """Serve read-only status plus authenticated managed-playlist administration."""

    operational_status: OperationalStatus | None = None
    engine_scheduler: EngineScheduler | None = None
    engine_lifecycle: EngineLifecycleController | None = None
    auth_synchronization: AuthSynchronization | None = None
    configuration_synchronization: ConfigurationSynchronization | None = None
    managed_admin_service: ManagedAdminService | None = None
    managed_admin_auth: SpotifyAuthProvider | None = None
    diagnostic_store: DiagnosticEventStore | None = None

    def _send_headers(
        self,
        *,
        content_type: str,
        length: int,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        if extra_headers is not None:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()

    def _spotify_state(self) -> AuthorizationState | None:
        synchronization = self.auth_synchronization
        if synchronization is None:
            return super()._spotify_state()
        with synchronization.hold():
            return super()._spotify_state()

    def _handle_spotify_callback(self, query: str) -> None:
        synchronization = self.auth_synchronization
        if synchronization is None:
            super()._handle_spotify_callback(query)
            return
        with synchronization.hold():
            super()._handle_spotify_callback(query)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, b"Invalid request")
            return
        if parsed.path in _MANAGED_POST_PATHS and self.managed_admin_service is not None:
            self._handle_managed_post(parsed.path)
            return

        before = self._spotify_state()
        synchronization = self.auth_synchronization
        if synchronization is None:
            super().do_POST()
        else:
            with synchronization.hold():
                super().do_POST()
        after = self._spotify_state()
        if before is not AuthorizationState.CONNECTED and after is AuthorizationState.CONNECTED:
            lifecycle = self.engine_lifecycle
            if lifecycle is not None:
                lifecycle.wake()
                self.__class__.engine_scheduler = lifecycle.scheduler
            elif self.engine_scheduler is not None:
                self.engine_scheduler.wake()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, b"Invalid request")
            return

        if parsed.path == "/":
            ready = _data_dir_ready(self.data_dir)
            payload = _operational_status_page(
                ready=ready,
                spotify_state=self._spotify_state(),
                status=self.operational_status,
            )
            self._reply(
                HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
                payload,
            )
            return

        if parsed.path.startswith("/admin/covers/"):
            self._serve_bundled_cover(parsed.path)
            return

        if parsed.path == "/admin/" and self.managed_admin_service is not None:
            self._serve_managed_admin(parsed.query)
            return

        if parsed.path == "/admin/spotify/callback":
            before = self._spotify_state()
            super().do_GET()
            if before is not AuthorizationState.CONNECTED:
                after = self._spotify_state()
                if after is AuthorizationState.CONNECTED:
                    lifecycle = self.engine_lifecycle
                    if lifecycle is not None:
                        lifecycle.wake()
                        self.__class__.engine_scheduler = lifecycle.scheduler
                    elif self.engine_scheduler is not None:
                        self.engine_scheduler.wake()
            return

        super().do_GET()

    def _serve_managed_admin(self, query: str = "") -> None:
        security = self._require_admin()
        if security is None:
            return
        service = self.managed_admin_service
        if service is None:
            self._reply(
                HTTPStatus.NOT_FOUND,
                b"Not found",
                content_type="text/plain; charset=utf-8",
            )
            return
        synchronization = self.configuration_synchronization
        try:
            if synchronization is None:
                snapshot = service.snapshot()
            else:
                with synchronization.hold():
                    snapshot = service.snapshot()
        except ManagedStateError, OSError:
            self._reply(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                b"Managed playlist state could not be loaded safely",
                content_type="text/plain; charset=utf-8",
            )
            return
        status = self.operational_status
        last_cycle = None if status is None else status.snapshot().last_cycle
        notice_values = urllib.parse.parse_qs(query, keep_blank_values=True).get("notice", [])
        notice = _ADMIN_NOTICE_MESSAGES.get(notice_values[0]) if len(notice_values) == 1 else None
        payload = render_managed_admin_page(
            snapshot=snapshot,
            catalog=service.catalog,
            spotify_state=self._spotify_state(),
            csrf_token=security.issue_csrf_token(),
            last_cycle=last_cycle,
            lan_mode=self._is_lan_admin_mode(),
            notice=notice,
        )
        self._reply(HTTPStatus.OK, payload)

    def _serve_bundled_cover(self, path: str) -> None:
        if self._require_admin() is None:
            return
        prefix = "/admin/covers/"
        filename = urllib.parse.unquote(path[len(prefix) :])
        if not filename.endswith(".jpg") or "/" in filename or "\\" in filename:
            self._reply(
                HTTPStatus.NOT_FOUND,
                b"Not found",
                content_type="text/plain; charset=utf-8",
            )
            return
        cover_id = filename[:-4]
        allowed = {template.cover_id for template in BUILTIN_CATALOG.playlists}
        if cover_id not in allowed:
            self._reply(
                HTTPStatus.NOT_FOUND,
                b"Not found",
                content_type="text/plain; charset=utf-8",
            )
            return
        cover_path = _bundled_cover_path(filename)
        if cover_path is None:
            self._reply(
                HTTPStatus.NOT_FOUND,
                b"Not found",
                content_type="text/plain; charset=utf-8",
            )
            return
        try:
            payload = cover_path.read_bytes()
        except OSError:
            self._reply(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                b"Cover could not be read",
                content_type="text/plain; charset=utf-8",
            )
            return
        self._reply(HTTPStatus.OK, payload, content_type="image/jpeg")

    def _handle_managed_post(self, path: str) -> None:
        security = self._require_admin()
        if security is None:
            return
        service = self.managed_admin_service
        synchronization = self.configuration_synchronization
        lifecycle = self.engine_lifecycle
        if service is None or synchronization is None or lifecycle is None:
            self._reply(
                HTTPStatus.SERVICE_UNAVAILABLE,
                b"Managed playlist administration is not available",
                content_type="text/plain; charset=utf-8",
            )
            return
        try:
            form = self._read_form()
        except ValueError as exc:
            self._reply(
                HTTPStatus.BAD_REQUEST,
                str(exc).encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )
            return
        csrf_values = form.get("csrf_token", [])
        if len(csrf_values) != 1 or not security.consume_csrf_token(csrf_values[0]):
            self._reply(
                HTTPStatus.FORBIDDEN,
                b"Administration form validation failed",
                content_type="text/plain; charset=utf-8",
            )
            return

        try:
            if path == "/admin/playlists/sync":
                with synchronization.hold():
                    playlist_id = playlist_id_from_form(form)
                    if not any(
                        playlist.id == playlist_id for playlist in service.snapshot().managed
                    ):
                        raise ManagedAdminError(f"unknown managed playlist: {playlist_id}")
                # HTTPServer serves admin requests serially. Validate immutable managed state
                # under the configuration lock, then keep Spotify I/O outside that lock so a
                # slow metadata/cover request cannot delay an engine cycle.
                self._sync_managed_playlist(service, form)
            else:
                with synchronization.hold():
                    if path == "/admin/playlists/activate":
                        self._activate_managed_playlist(service, form)
                    elif path == "/admin/playlists/update":
                        self._update_managed_playlist(service, form)
                    else:
                        service.stop_managing(playlist_id_from_form(form))
                    configured = any(playlist.enabled for playlist in service.snapshot().managed)
                lifecycle.reconcile(configured=configured)
                self.__class__.engine_scheduler = lifecycle.scheduler
        except ManagedStateError:
            self._managed_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Managed playlist state could not be changed safely",
            )
            return
        except ValueError as exc:
            self._managed_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except SpotifyPlaylistSyncError as exc:
            self._managed_error(HTTPStatus.BAD_GATEWAY, str(exc))
            return
        except ManagedAdminError as exc:
            self._managed_error(HTTPStatus.CONFLICT, str(exc))
            return
        except SpotifyAuthError:
            self._managed_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Spotify authorization is unavailable; reconnect Spotify and retry",
            )
            return
        except SpotifyApiError, SpotifyTransportError:
            self._managed_error(
                HTTPStatus.BAD_GATEWAY,
                "Spotify could not apply the playlist change; local state was preserved",
            )
            return
        except OSError, RuntimeError:
            self._managed_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Managed playlist state could not be changed safely",
            )
            return

        location = (
            "/admin/?notice=spotify-sync-applied" if path == "/admin/playlists/sync" else "/admin/"
        )
        self._reply(
            HTTPStatus.SEE_OTHER,
            b"",
            extra_headers={"Location": location},
        )

    def _activate_managed_playlist(
        self,
        service: ManagedAdminService,
        form: Mapping[str, list[str]],
    ) -> None:
        auth = self.managed_admin_auth
        if auth is None:
            raise ManagedAdminError("Spotify authorization is required to create a playlist")
        access_token = auth.get_access_token()
        service.activate(
            template_id=single_form_value(form, "template_id"),
            display_name=single_form_value(form, "display_name"),
            description=single_form_value(form, "description", required=False),
            cover_id=single_form_value(form, "cover_id"),
            source_ids=form.get("source_id", []),
            access_token=access_token,
        )

    def _update_managed_playlist(
        self,
        service: ManagedAdminService,
        form: Mapping[str, list[str]],
    ) -> None:
        playlist_id = playlist_id_from_form(form)
        name = single_form_value(form, "display_name")
        description = single_form_value(form, "description", required=False)
        cover_id = single_form_value(form, "cover_id")
        enabled_values = form.get("enabled", [])
        if enabled_values not in ([], ["1"]):
            raise ValueError("enabled must be omitted or set exactly once")
        enabled = bool(enabled_values)

        snapshot = service.snapshot()
        current = next(
            (playlist for playlist in snapshot.managed if playlist.id == playlist_id),
            None,
        )
        if current is None:
            raise ManagedAdminError(f"unknown managed playlist: {playlist_id}")
        metadata_changed = (
            name.strip() != current.display_name or description != current.description
        )
        access_token: str | None = None
        if metadata_changed:
            auth = self.managed_admin_auth
            if auth is None:
                raise ManagedAdminError(
                    "Spotify must be connected to change playlist name or description"
                )
            access_token = auth.get_access_token()
        service.update(
            playlist_id,
            display_name=name,
            description=description,
            cover_id=cover_id,
            source_ids=form.get("source_id", []),
            enabled=enabled,
            access_token=access_token,
        )

    def _sync_managed_playlist(
        self,
        service: ManagedAdminService,
        form: Mapping[str, list[str]],
    ) -> None:
        auth = self.managed_admin_auth
        if auth is None:
            raise ManagedAdminError("Spotify must be connected to apply metadata and cover")
        service.sync_spotify_metadata_and_cover(
            playlist_id_from_form(form),
            access_token=auth.get_access_token(),
        )

    def _managed_error(self, status: HTTPStatus, message: str) -> None:
        payload = (
            "<!doctype html><meta charset='utf-8'><title>Administration error</title>"
            f"<p>{html.escape(message)}</p><p><a href='/admin/'>Return to administration</a></p>"
        ).encode()
        self._reply(status, payload)


def serve(
    *,
    host: str = DEFAULT_HEALTH_HOST,
    port: int = DEFAULT_HEALTH_PORT,
    data_dir: Path = DEFAULT_DATA_DIR,
    stop_event: threading.Event | None = None,
    environ: Mapping[str, str] | None = None,
    interval: timedelta | None = None,
) -> int:
    """Run HTTP, OAuth and the production engine scheduler in one durable process."""
    env = os.environ if environ is None else environ
    database_path = initialize_runtime_storage(data_dir)
    store = SQLiteStore(database_path)
    diagnostic_candidate = DiagnosticEventStore(database_path)
    try:
        diagnostic_candidate.initialize()
    except PersistenceError:
        diagnostic_store: DiagnosticEventStore | None = None
    else:
        diagnostic_store = diagnostic_candidate
    diagnostics = OperationalDiagnostics(diagnostic_store)
    if diagnostic_store is None:
        diagnostics.emit(
            occurred_at=datetime.now(UTC),
            severity=DiagnosticSeverity.ERROR,
            component="diagnostics",
            event_name="diagnostic_store_unavailable",
            details={"phase": "persistence"},
        )

    admin_security, spotify_auth = build_engine_runtime_auth(data_dir, environ=env)
    config = _load_runtime_config(data_dir, env)
    configured = config is not None
    if configured and spotify_auth is None:
        raise RuntimeError(
            "engine configuration requires production Spotify authorization settings "
            "or explicit LAN development authorization"
        )

    scheduler_interval = interval if interval is not None else _runtime_interval(env)
    auth_synchronization = AuthSynchronization()
    configuration_synchronization = ConfigurationSynchronization()
    auth_provider = (
        None if spotify_auth is None else _LockedAuthProvider(spotify_auth, auth_synchronization)
    )
    lifecycle: EngineLifecycleController | None = None
    if auth_provider is not None:
        base_runner = ReloadingEngineCycleRunner(
            data_dir,
            env,
            store,
            auth_provider,
            configuration_synchronization,
        )
        runner = InstrumentedEngineCycleRunner(base_runner, diagnostics)
        lifecycle = EngineLifecycleController(
            runner,
            interval=scheduler_interval,
            diagnostics=diagnostics,
        )
        with configuration_synchronization.hold():
            lifecycle.reconcile(configured=configured)
        status: OperationalStatus = lifecycle.status
    else:
        status = _MutableOperationalStatus(configured=configured)

    managed_admin_service = _build_managed_admin_service(
        data_dir,
        env,
        spotify_auth=spotify_auth,
    )

    OperationalHealthHandler.data_dir = data_dir
    OperationalHealthHandler.admin_security = admin_security
    OperationalHealthHandler.spotify_auth = spotify_auth
    OperationalHealthHandler.operational_status = status
    OperationalHealthHandler.engine_lifecycle = lifecycle
    OperationalHealthHandler.engine_scheduler = None if lifecycle is None else lifecycle.scheduler
    OperationalHealthHandler.auth_synchronization = auth_synchronization
    OperationalHealthHandler.configuration_synchronization = configuration_synchronization
    OperationalHealthHandler.managed_admin_service = managed_admin_service
    OperationalHealthHandler.managed_admin_auth = auth_provider
    OperationalHealthHandler.diagnostic_store = diagnostic_store
    server = HTTPServer((host, port), OperationalHealthHandler)
    server.timeout = 0.5
    stopped = stop_event if stop_event is not None else threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stopped.set()
        if lifecycle is not None:
            lifecycle.wake()

    handlers_installed = (
        stop_event is None and threading.current_thread() is threading.main_thread()
    )
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    if handlers_installed:
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    diagnostics.emit(
        occurred_at=datetime.now(UTC),
        severity=DiagnosticSeverity.INFO,
        component="runtime",
        event_name="runtime_ready",
        details={"next_state": "running"},
    )
    try:
        while not stopped.is_set():
            server.handle_request()
    finally:
        diagnostics.emit(
            occurred_at=datetime.now(UTC),
            severity=DiagnosticSeverity.INFO,
            component="runtime",
            event_name="runtime_stopping",
            details={"next_state": "stopped"},
        )
        stopped.set()
        server.server_close()
        if lifecycle is not None:
            lifecycle.shutdown()
        OperationalHealthHandler.engine_scheduler = None
        OperationalHealthHandler.engine_lifecycle = None
        OperationalHealthHandler.auth_synchronization = None
        OperationalHealthHandler.configuration_synchronization = None
        OperationalHealthHandler.managed_admin_service = None
        OperationalHealthHandler.managed_admin_auth = None
        OperationalHealthHandler.diagnostic_store = None
        if handlers_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)
        diagnostics.emit(
            occurred_at=datetime.now(UTC),
            severity=DiagnosticSeverity.INFO,
            component="runtime",
            event_name="runtime_stopped",
            details={"next_state": "stopped"},
        )
    return 0


def _build_managed_admin_service(
    data_dir: Path,
    environ: Mapping[str, str],
    *,
    spotify_auth: SpotifyAuthService | None,
) -> ManagedAdminService | None:
    if spotify_auth is None:
        return None
    explicit = environ.get(CONFIG_PATH_ENV)
    if explicit is not None and explicit.strip():
        return None
    legacy_path = data_dir / DEFAULT_CONFIG_FILENAME
    if legacy_path.is_symlink() or legacy_path.exists():
        return None
    return ManagedAdminService(
        ManagedStateStore(data_dir / MANAGED_STATE_FILENAME),
        cover_loader=_load_bundled_cover,
    )


def _load_bundled_cover(cover_id: str) -> bytes:
    cover_path = _bundled_cover_path(f"{cover_id}.jpg")
    if cover_path is None:
        raise FileNotFoundError("bundled playlist cover is unavailable")
    return cover_path.read_bytes()


def _bundled_cover_path(filename: str) -> Path | None:
    candidates = (
        _BUNDLED_COVER_DIR / filename,
        Path(__file__).resolve().parents[2] / "assets" / "covers" / "spotify" / filename,
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _load_runtime_config(data_dir: Path, environ: Mapping[str, str]) -> EngineConfig | None:
    return load_effective_config(data_dir, environ)


def _runtime_interval(environ: Mapping[str, str]) -> timedelta:
    raw = environ.get(_INTERVAL_SECONDS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_ENGINE_INTERVAL
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{_INTERVAL_SECONDS_ENV} must be an integer") from exc
    interval = timedelta(seconds=seconds)
    if interval < _MIN_RUNTIME_INTERVAL:
        raise RuntimeError(f"{_INTERVAL_SECONDS_ENV} must be at least 60 seconds")
    return interval


def _operational_status_page(
    *,
    ready: bool,
    spotify_state: AuthorizationState | None,
    status: OperationalStatus | None,
) -> bytes:
    snapshot = (
        OperationalStatus(configured=False).snapshot() if status is None else status.snapshot()
    )
    runtime_label = "Ready" if ready else "Degraded"
    storage_label = "Writable" if ready else "Unavailable"
    engine_label = _engine_label(snapshot)
    cycle = snapshot.last_cycle
    cycle_result = "Not run yet" if cycle is None else ("Success" if cycle.ok else "Failed")
    cycle_started = "—" if cycle is None else _format_time(cycle.started_at)
    cycle_finished = "—" if cycle is None else _format_time(cycle.finished_at)
    cycle_error = "—" if cycle is None or cycle.error is None else html.escape(cycle.error)
    next_run = "—" if snapshot.next_run_at is None else _format_time(snapshot.next_run_at)
    source_rows = _source_rows(cycle)
    playlist_rows = _playlist_rows(cycle)

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>News Bulletin Playlists</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 72rem; margin: 3rem auto;
           padding: 0 1.25rem; line-height: 1.5; }}
    dl {{ display: grid; grid-template-columns: max-content 1fr; gap: .4rem 1rem; }}
    dt {{ font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; margin: 1rem 0 2rem; }}
    th, td {{ text-align: left; vertical-align: top; padding: .45rem .55rem;
              border-bottom: 1px solid #ddd; }}
    code {{ font-family: ui-monospace, monospace; }}
  </style>
</head>
<body>
  <h1>News Bulletin Playlists</h1>
  <p>This page is read-only. Administrative actions remain isolated under the
     authenticated <code>/admin/</code> surface.</p>
  <dl>
    <dt>Runtime</dt><dd>{runtime_label}</dd>
    <dt>Persistent storage</dt><dd>{storage_label}</dd>
    <dt>Spotify authorization</dt><dd>{_spotify_label(spotify_state)}</dd>
    <dt>Engine</dt><dd>{engine_label}</dd>
    <dt>Last cycle</dt><dd>{cycle_result}</dd>
    <dt>Cycle start</dt><dd>{cycle_started}</dd>
    <dt>Cycle end</dt><dd>{cycle_finished}</dd>
    <dt>Next run</dt><dd>{next_run}</dd>
    <dt>Cycle error</dt><dd>{cycle_error}</dd>
    <dt>Version</dt><dd><code>{html.escape(__version__)}</code></dd>
  </dl>
  <h2>Sources</h2>
  <table>
    <thead><tr>
      <th>Source</th><th>Result</th><th>Last success</th><th>Editions</th><th>Error</th>
    </tr></thead>
    <tbody>{source_rows}</tbody>
  </table>
  <h2>Playlists</h2>
  <table>
    <thead><tr>
      <th>Playlist</th><th>Result</th><th>Last success</th><th>Items</th>
      <th>Write</th><th>Error</th>
    </tr></thead>
    <tbody>{playlist_rows}</tbody>
  </table>
</body>
</html>
"""
    return document.encode("utf-8")


def _engine_label(snapshot: OperationalStatusSnapshot) -> str:
    if not snapshot.configured:
        return "Not configured"
    return "Cycle running" if snapshot.running else "Scheduled"


def _source_rows(cycle: EngineCycleResult | None) -> str:
    if cycle is None or not cycle.sources:
        return '<tr><td colspan="5">No source cycle data yet.</td></tr>'
    rows = []
    for source in cycle.sources:
        result = "Success" if source.ok else "Failed"
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(source.source_id))}</code></td>"
            f"<td>{result}</td>"
            f"<td>{_format_optional_time(source.last_success_at)}</td>"
            f"<td>{source.edition_count} fetched / {source.matched_count} matched</td>"
            f"<td>{_escape_optional(source.error)}</td>"
            "</tr>"
        )
    return "".join(rows)


def _playlist_rows(cycle: EngineCycleResult | None) -> str:
    if cycle is None or not cycle.playlists:
        return '<tr><td colspan="6">No playlist cycle data yet.</td></tr>'
    rows = []
    for playlist in cycle.playlists:
        result = "Success" if playlist.ok else "Failed"
        applied = "unverified" if playlist.applied_count is None else str(playlist.applied_count)
        write = "—" if playlist.wrote is None else ("updated" if playlist.wrote else "unchanged")
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(playlist.playlist_id))}</code></td>"
            f"<td>{result}</td>"
            f"<td>{_format_optional_time(playlist.last_success_at)}</td>"
            f"<td>{playlist.desired_count} desired / {applied} verified</td>"
            f"<td>{write}</td>"
            f"<td>{_escape_optional(playlist.error)}</td>"
            "</tr>"
        )
    return "".join(rows)


def _spotify_label(state: AuthorizationState | None) -> str:
    if state is None:
        return "Not configured"
    return {
        AuthorizationState.DISCONNECTED: "Not connected",
        AuthorizationState.CONNECTED: "Connected",
        AuthorizationState.REAUTH_REQUIRED: "Reauthorization required",
        AuthorizationState.ERROR: "Authorization state error",
    }[state]


def _format_optional_time(value: datetime | None) -> str:
    return "—" if value is None else _format_time(value)


def _format_time(value: datetime) -> str:
    timestamp = value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return html.escape(timestamp)


def _escape_optional(value: str | None) -> str:
    return "—" if value is None else html.escape(value)
