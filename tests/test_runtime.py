from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path

from news_bulletin_playlist.runtime import HealthHandler, ensure_data_dir, healthcheck


def _serve_one(server: HTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    return thread


def test_ensure_data_dir_creates_and_verifies_writable_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ensure_data_dir(data_dir)
    assert data_dir.is_dir()


def test_health_endpoint_reports_ok_for_writable_data(tmp_path: Path) -> None:
    HealthHandler.data_dir = tmp_path
    server = HTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = _serve_one(server)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/healthz", timeout=2
        ) as response:
            assert response.status == HTTPStatus.OK
            payload = json.loads(response.read())
            assert payload["status"] == "ok"
    finally:
        thread.join(timeout=2)
        server.server_close()


def test_health_endpoint_fails_closed_when_data_path_is_missing(tmp_path: Path) -> None:
    HealthHandler.data_dir = tmp_path / "missing"
    server = HTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = _serve_one(server)
    try:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/healthz", timeout=2
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == HTTPStatus.SERVICE_UNAVAILABLE
        else:
            raise AssertionError("health endpoint unexpectedly succeeded")
    finally:
        thread.join(timeout=2)
        server.server_close()


def test_healthcheck_accepts_healthy_runtime(tmp_path: Path) -> None:
    HealthHandler.data_dir = tmp_path
    server = HTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = _serve_one(server)
    try:
        assert healthcheck(url=f"http://127.0.0.1:{server.server_port}/healthz") == 0
    finally:
        thread.join(timeout=2)
        server.server_close()
