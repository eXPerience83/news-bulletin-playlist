"""Minimal long-lived runtime host for container deployments."""

from __future__ import annotations

import html
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


def _status_page(*, ready: bool) -> bytes:
    """Render the intentionally read-only P0 web portal."""
    status = "Ready" if ready else "Degraded"
    storage = "Writable" if ready else "Unavailable"
    version = html.escape(__version__)
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>News Bulletin Playlists</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 48rem; margin: 4rem auto;
           padding: 0 1.25rem; line-height: 1.5; }}
    dl {{ display: grid; grid-template-columns: max-content 1fr; gap: .5rem 1rem; }}
    dt {{ font-weight: 700; }}
    code {{ font-family: ui-monospace, monospace; }}
  </style>
</head>
<body>
  <h1>News Bulletin Playlists</h1>
  <p>The container runtime is running. This P0 page is read-only; playlist controls
     will be added only after the engine and authentication model are implemented.</p>
  <dl>
    <dt>Runtime</dt><dd>{status}</dd>
    <dt>Persistent storage</dt><dd>{storage}</dd>
    <dt>Version</dt><dd><code>{version}</code></dd>
  </dl>
</body>
</html>
"""
    return document.encode("utf-8")


class HealthHandler(BaseHTTPRequestHandler):
    """Serve the status portal and Docker health endpoint without request logging."""

    data_dir = DEFAULT_DATA_DIR
    timeout = 2.0

    def _send_headers(self, *, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            "frame-ancestors 'none'",
        )
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        ready = _data_dir_ready(self.data_dir)

        if self.path == "/":
            payload = _status_page(ready=ready)
            self.send_response(HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE)
            self._send_headers(content_type="text/html; charset=utf-8", length=len(payload))
            self.wfile.write(payload)
            return

        if self.path == "/healthz":
            payload = json.dumps(
                {"status": "ok" if ready else "degraded", "version": __version__},
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE)
            self._send_headers(content_type="application/json", length=len(payload))
            self.wfile.write(payload)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def version_string(self) -> str:
        """Avoid disclosing the Python runtime version in HTTP headers."""
        return "news-bulletin-playlist"

    def log_message(self, format: str, *args: object) -> None:
        """Suppress request logging for the small built-in runtime server."""
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
        f"news-bulletin-playlist runtime ready; web=http://{host}:{server.server_port}/ "
        f"health=http://127.0.0.1:{server.server_port}/healthz data={data_dir}",
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
