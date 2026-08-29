"""Minimal long-lived runtime host for container deployments."""

from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import FrameType

from news_bulletin_playlist import __version__

DEFAULT_DATA_DIR = Path("/data")
DEFAULT_HEALTH_HOST = "127.0.0.1"
DEFAULT_HEALTH_PORT = 8080
DEFAULT_HEALTH_URL = f"http://{DEFAULT_HEALTH_HOST}:{DEFAULT_HEALTH_PORT}/healthz"


def _data_dir_ready(data_dir: Path) -> bool:
    """Return whether the persistent path accepts a real durable write."""
    if not data_dir.is_dir():
        return False
    try:
        with tempfile.NamedTemporaryFile(prefix=".runtime-health-", dir=data_dir) as probe:
            probe.write(b"ok")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError:
        return False
    return True


def ensure_data_dir(data_dir: Path) -> None:
    """Fail early unless the persistent application directory is writable."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"application data directory is not writable: {data_dir}") from exc
    if not _data_dir_ready(data_dir):
        raise RuntimeError(f"application data directory is not writable: {data_dir}")


class HealthHandler(BaseHTTPRequestHandler):
    """Local-only operational health endpoint kept as part of the future engine host."""

    data_dir = DEFAULT_DATA_DIR
    timeout = 2.0

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/healthz":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        ready = _data_dir_ready(self.data_dir)
        payload = json.dumps(
            {"status": "ok" if ready else "degraded", "version": __version__},
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress localhost health-request logging."""
        return


def serve(
    *,
    host: str = DEFAULT_HEALTH_HOST,
    port: int = DEFAULT_HEALTH_PORT,
    data_dir: Path = DEFAULT_DATA_DIR,
    stop_event: threading.Event | None = None,
) -> int:
    """Run the durable container host until SIGTERM/SIGINT requests shutdown."""
    ensure_data_dir(data_dir)
    HealthHandler.data_dir = data_dir
    server = HTTPServer((host, port), HealthHandler)
    server.timeout = 0.5
    stopped = stop_event if stop_event is not None else threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stopped.set()

    handlers_installed = (
        stop_event is None and threading.current_thread() is threading.main_thread()
    )
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    if handlers_installed:
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    print(
        f"news-bulletin-playlist runtime ready; health=http://{host}:{server.server_port}/healthz "
        f"data={data_dir}",
        flush=True,
    )
    try:
        while not stopped.is_set():
            server.handle_request()
    finally:
        server.server_close()
        if handlers_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)
    return 0


def healthcheck(*, url: str = DEFAULT_HEALTH_URL, timeout: float = 2.0) -> int:
    """Return a Docker-healthcheck-compatible status code."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != HTTPStatus.OK:
                return 1
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return 1
    return 0 if isinstance(payload, dict) and payload.get("status") == "ok" else 1
