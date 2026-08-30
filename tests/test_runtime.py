from __future__ import annotations

import base64
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest

import news_bulletin_playlist.runtime as runtime
from news_bulletin_playlist.persistence import (
    DEFAULT_DB_FILENAME,
    LATEST_SCHEMA_VERSION,
    SQLiteStore,
)
from news_bulletin_playlist.runtime import (
    AdminSecurity,
    HealthHandler,
    build_runtime_auth,
    ensure_data_dir,
    healthcheck,
)
from news_bulletin_playlist.spotify.auth import (
    PRODUCTION_SCOPES,
    AuthorizationState,
    SpotifyAuthService,
    SpotifyCredentialStore,
    TokenResponse,
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class _FakeTokenTransport:
    def exchange_code(
        self,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> TokenResponse:
        assert client_id == "client-id"
        assert code == "authorization-code"
        assert redirect_uri == "https://news.example.test/admin/spotify/callback"
        assert verifier
        return TokenResponse(
            access_token="access-secret",
            expires_in=3600,
            granted_scopes=PRODUCTION_SCOPES,
            refresh_token="refresh-secret",
        )

    def refresh_token(self, *, client_id: str, refresh_token: str) -> TokenResponse:
        raise AssertionError("refresh not expected in runtime Web UI test")


@pytest.fixture(autouse=True)
def _reset_handler_auth() -> None:
    previous_admin = HealthHandler.admin_security
    previous_spotify = HealthHandler.spotify_auth
    HealthHandler.admin_security = None
    HealthHandler.spotify_auth = None
    yield
    HealthHandler.admin_security = previous_admin
    HealthHandler.spotify_auth = previous_spotify


def _serve_one(server: HTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    return thread


def _basic_header(password: str) -> str:
    encoded = base64.b64encode(f"admin:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


def _no_redirect_open(request: urllib.request.Request) -> urllib.error.HTTPError:
    opener = urllib.request.build_opener(_NoRedirect())
    with pytest.raises(urllib.error.HTTPError) as raised:
        opener.open(request, timeout=2)
    return raised.value


def test_ensure_data_dir_creates_and_verifies_writable_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ensure_data_dir(data_dir)
    assert data_dir.is_dir()


def test_initialize_runtime_storage_creates_migrated_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    database_path = runtime.initialize_runtime_storage(data_dir)
    runtime.initialize_runtime_storage(data_dir)

    assert database_path == data_dir / DEFAULT_DB_FILENAME
    assert database_path.is_file()
    assert SQLiteStore(database_path).schema_version() == LATEST_SCHEMA_VERSION


def test_runtime_auth_is_disabled_by_default(tmp_path: Path) -> None:
    admin, spotify = build_runtime_auth(tmp_path, environ={})
    assert admin is None
    assert spotify is None


def test_runtime_spotify_auth_requires_complete_secure_configuration(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must be configured together"):
        build_runtime_auth(tmp_path, environ={"SPOTIFY_CLIENT_ID": "client-id"})

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        build_runtime_auth(
            tmp_path,
            environ={
                "SPOTIFY_CLIENT_ID": "client-id",
                "NEWS_PLAYLIST_EXTERNAL_URL": "https://news.example.test",
            },
        )

    with pytest.raises(RuntimeError, match="invalid Spotify"):
        build_runtime_auth(
            tmp_path,
            environ={
                "SPOTIFY_CLIENT_ID": "client-id",
                "NEWS_PLAYLIST_EXTERNAL_URL": "http://192.168.1.20:8788",
                "NEWS_PLAYLIST_ADMIN_PASSWORD": "long-enough-admin-password",
            },
        )


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
            assert "Spotify authorization" in body
            assert "Client Secret" not in body
            assert "Connect Spotify" not in body
            assert "SPOTIFY_CLIENT_ID" not in body
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Server"] == "news-bulletin-playlist"
    finally:
        thread.join(timeout=2)
        server.server_close()


def test_admin_surface_is_absent_when_not_configured(tmp_path: Path) -> None:
    HealthHandler.data_dir = tmp_path
    server = HTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = _serve_one(server)
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/admin/", timeout=2
            )
        assert raised.value.code == HTTPStatus.NOT_FOUND
    finally:
        thread.join(timeout=2)
        server.server_close()


def test_admin_surface_requires_basic_authentication(tmp_path: Path) -> None:
    HealthHandler.data_dir = tmp_path
    HealthHandler.admin_security = AdminSecurity("long-enough-admin-password")
    server = HTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = _serve_one(server)
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/admin/", timeout=2
            )
        assert raised.value.code == HTTPStatus.UNAUTHORIZED
        assert raised.value.headers["WWW-Authenticate"].startswith("Basic ")
    finally:
        thread.join(timeout=2)
        server.server_close()


def test_admin_connect_requires_csrf(tmp_path: Path) -> None:
    password = "long-enough-admin-password"
    HealthHandler.data_dir = tmp_path
    HealthHandler.admin_security = AdminSecurity(password)
    HealthHandler.spotify_auth = SpotifyAuthService(
        client_id="client-id",
        redirect_uri="https://news.example.test/admin/spotify/callback",
        store=SpotifyCredentialStore(tmp_path / "spotify-auth.json"),
        transport=_FakeTokenTransport(),
    )
    server = HTTPServer(("127.0.0.1", 0), HealthHandler)
    try:
        payload = urllib.parse.urlencode({"csrf_token": "wrong"}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/admin/spotify/connect",
            data=payload,
            headers={
                "Authorization": _basic_header(password),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        thread = _serve_one(server)
        try:
            with pytest.raises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            assert raised.value.code == HTTPStatus.FORBIDDEN
        finally:
            thread.join(timeout=2)
    finally:
        server.server_close()


def test_admin_connect_and_callback_complete_without_manual_url_paste(tmp_path: Path) -> None:
    password = "long-enough-admin-password"
    store = SpotifyCredentialStore(tmp_path / "spotify-auth.json")
    HealthHandler.data_dir = tmp_path
    HealthHandler.admin_security = AdminSecurity(password)
    HealthHandler.spotify_auth = SpotifyAuthService(
        client_id="client-id",
        redirect_uri="https://news.example.test/admin/spotify/callback",
        store=store,
        transport=_FakeTokenTransport(),
    )
    server = HTTPServer(("127.0.0.1", 0), HealthHandler)
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        admin_request = urllib.request.Request(
            f"{base_url}/admin/",
            headers={"Authorization": _basic_header(password)},
        )
        thread = _serve_one(server)
        try:
            with urllib.request.urlopen(admin_request, timeout=2) as response:
                body = response.read().decode("utf-8")
        finally:
            thread.join(timeout=2)
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', body)
        assert csrf_match is not None

        connect_request = urllib.request.Request(
            f"{base_url}/admin/spotify/connect",
            data=urllib.parse.urlencode({"csrf_token": csrf_match.group(1)}).encode(),
            headers={
                "Authorization": _basic_header(password),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        thread = _serve_one(server)
        try:
            redirect = _no_redirect_open(connect_request)
        finally:
            thread.join(timeout=2)
        assert redirect.code == HTTPStatus.SEE_OTHER
        authorize_url = redirect.headers["Location"]
        authorize_params = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)
        state = authorize_params["state"][0]
        assert authorize_params["redirect_uri"] == [
            "https://news.example.test/admin/spotify/callback"
        ]

        callback_query = urllib.parse.urlencode(
            {"state": state, "code": "authorization-code"}
        )
        callback_request = urllib.request.Request(
            f"{base_url}/admin/spotify/callback?{callback_query}"
        )
        thread = _serve_one(server)
        try:
            callback_redirect = _no_redirect_open(callback_request)
        finally:
            thread.join(timeout=2)
        assert callback_redirect.code == HTTPStatus.SEE_OTHER
        assert callback_redirect.headers["Location"] == "/admin/"

        assert HealthHandler.spotify_auth is not None
        assert (
            HealthHandler.spotify_auth.authorization_state()
            is AuthorizationState.CONNECTED
        )
        persisted = store.path.read_text(encoding="utf-8")
        assert "refresh-secret" in persisted
        assert "access-secret" not in persisted
    finally:
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
