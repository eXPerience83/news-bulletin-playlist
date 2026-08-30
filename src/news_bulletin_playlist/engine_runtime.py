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
from news_bulletin_playlist.config import ConfigError, load_config
from news_bulletin_playlist.engine import (
    DEFAULT_ENGINE_INTERVAL,
    EngineCycleResult,
    EngineRunner,
    EngineScheduler,
    OperationalStatus,
    OperationalStatusSnapshot,
    SpotifyAuthProvider,
)
from news_bulletin_playlist.lan_admin import LanAdminHandler, build_engine_runtime_auth
from news_bulletin_playlist.models import EngineConfig
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.runtime import (
    DEFAULT_DATA_DIR,
    DEFAULT_HEALTH_HOST,
    DEFAULT_HEALTH_PORT,
    _data_dir_ready,
    initialize_runtime_storage,
)
from news_bulletin_playlist.spotify.auth import AuthorizationState, SpotifyAuthService

DEFAULT_CONFIG_FILENAME = "news-bulletin-playlist.yaml"
_CONFIG_PATH_ENV = "NEWS_PLAYLIST_CONFIG"
_INTERVAL_SECONDS_ENV = "NEWS_PLAYLIST_INTERVAL_SECONDS"
_MIN_RUNTIME_INTERVAL = timedelta(minutes=1)
_SCHEDULER_SHUTDOWN_WAIT_SECONDS = 10.0


class AuthSynchronization:
    """Serialize token refresh and Web UI authorization against one credential store."""

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


class ReloadingEngineCycleRunner:
    """Load and validate durable configuration immediately before every engine cycle."""

    def __init__(
        self,
        data_dir: Path,
        environ: Mapping[str, str],
        store: SQLiteStore,
        auth: SpotifyAuthProvider,
    ) -> None:
        self.data_dir = data_dir
        self.environ = environ
        self.store = store
        self.auth = auth

    def run_cycle(self) -> EngineCycleResult:
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
    """Extend the existing secure HTTP surface with read-only engine status."""

    operational_status: OperationalStatus | None = None
    engine_scheduler: EngineScheduler | None = None
    auth_synchronization: AuthSynchronization | None = None

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
        before = self._spotify_state()
        synchronization = self.auth_synchronization
        if synchronization is None:
            super().do_POST()
        else:
            with synchronization.hold():
                super().do_POST()
        after = self._spotify_state()
        scheduler = self.engine_scheduler
        if (
            before is not AuthorizationState.CONNECTED
            and after is AuthorizationState.CONNECTED
            and scheduler is not None
        ):
            scheduler.wake()

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

        if parsed.path == "/admin/spotify/callback":
            before = self._spotify_state()
            super().do_GET()
            if before is not AuthorizationState.CONNECTED:
                after = self._spotify_state()
                scheduler = self.engine_scheduler
                if after is AuthorizationState.CONNECTED and scheduler is not None:
                    scheduler.wake()
            return

        super().do_GET()


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
    admin_security, spotify_auth = build_engine_runtime_auth(data_dir, environ=env)
    config = _load_runtime_config(data_dir, env)
    configured = config is not None
    if configured and spotify_auth is None:
        raise RuntimeError(
            "engine configuration requires production Spotify authorization settings "
            "or explicit LAN development authorization"
        )

    scheduler_interval = interval if interval is not None else _runtime_interval(env)
    status = OperationalStatus(configured=configured)
    scheduler: EngineScheduler | None = None
    auth_synchronization = AuthSynchronization()
    if config is not None and spotify_auth is not None:
        auth_provider = _LockedAuthProvider(spotify_auth, auth_synchronization)
        scheduler = EngineScheduler(
            ReloadingEngineCycleRunner(data_dir, env, store, auth_provider),
            status,
            interval=scheduler_interval,
        )

    OperationalHealthHandler.data_dir = data_dir
    OperationalHealthHandler.admin_security = admin_security
    OperationalHealthHandler.spotify_auth = spotify_auth
    OperationalHealthHandler.operational_status = status
    OperationalHealthHandler.engine_scheduler = scheduler
    OperationalHealthHandler.auth_synchronization = auth_synchronization
    server = HTTPServer((host, port), OperationalHealthHandler)
    server.timeout = 0.5
    stopped = stop_event if stop_event is not None else threading.Event()
    scheduler_thread: threading.Thread | None = None

    def request_runtime_restart() -> None:
        stopped.set()
        if scheduler is not None:
            scheduler.wake()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        request_runtime_restart()

    OperationalHealthHandler.runtime_restart = request_runtime_restart
    handlers_installed = (
        stop_event is None and threading.current_thread() is threading.main_thread()
    )
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    if handlers_installed:
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    if scheduler is not None:
        scheduler_thread = threading.Thread(
            target=scheduler.run,
            args=(stopped,),
            name="news-playlist-engine",
            daemon=True,
        )
        scheduler_thread.start()

    auth_status = "enabled" if spotify_auth is not None else "disabled"
    engine_status = "enabled" if configured else "not-configured"
    print(
        f"news-bulletin-playlist runtime ready; web=http://{host}:{server.server_port}/ "
        f"health=http://127.0.0.1:{server.server_port}/healthz data={data_dir} "
        f"db={database_path} spotify_auth={auth_status} engine={engine_status}",
        flush=True,
    )
    try:
        while not stopped.is_set():
            server.handle_request()
    finally:
        stopped.set()
        if scheduler is not None:
            scheduler.wake()
        server.server_close()
        if scheduler_thread is not None:
            scheduler_thread.join(timeout=_SCHEDULER_SHUTDOWN_WAIT_SECONDS)
            if scheduler_thread.is_alive():
                print(
                    "news-bulletin-playlist engine shutdown exceeded graceful wait; exiting",
                    flush=True,
                )
        OperationalHealthHandler.runtime_restart = None
        OperationalHealthHandler.engine_scheduler = None
        OperationalHealthHandler.auth_synchronization = None
        if handlers_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)
    return 0


def _load_runtime_config(data_dir: Path, environ: Mapping[str, str]) -> EngineConfig | None:
    raw_path = environ.get(_CONFIG_PATH_ENV)
    explicit_path = raw_path.strip() if raw_path is not None and raw_path.strip() else None
    path = Path(explicit_path) if explicit_path is not None else data_dir / DEFAULT_CONFIG_FILENAME
    if not path.exists():
        if explicit_path is not None:
            raise RuntimeError(f"configured engine YAML does not exist: {path}")
        return None
    try:
        return load_config(path)
    except ConfigError as exc:
        raise RuntimeError(f"invalid engine configuration: {exc}") from exc


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
        OperationalStatus(configured=False).snapshot()
        if status is None
        else status.snapshot()
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
