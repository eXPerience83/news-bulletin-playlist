from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path

import pytest

import news_bulletin_playlist.runtime as runtime
from news_bulletin_playlist.runtime import HealthHandler, ensure_data_dir, healthcheck


def _serve_one(server: HTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    return thread


def test_ensure_data_dir_creates_and_verifies_writable_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ensure_data_dir(data_dir)
    assert data_dir.is_dir()


def test_status_portal_reports_runtime_without_sensitive_details(tmp_path: Path) -> None:
    HealthHandler.data_dir = tmp_path
    server = HTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = _serve_one(server)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/", timeout=2
        ) as response:
            assert response.status == HTTPStatus.OK
            body = response.read().decode("utf-8")
            assert "News Bulletin Playlists" in body
            assert "Persistent storage" in body
            assert "Client Secret" not in body
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Server"] == "news-bulletin-playlist"
    finally:
        thread.join(timeout=2)
        server.server_close()


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


def test_health_endpoint_fails_closed_when_real_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_write_probe(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("simulated storage failure")

    monkeypatch.setattr(runtime.tempfile, "NamedTemporaryFile", fail_write_probe)
    HealthHandler.data_dir = tmp_path
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
