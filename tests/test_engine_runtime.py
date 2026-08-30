from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.engine import (
    EngineCycleResult,
    OperationalStatus,
    PlaylistCycleOutcome,
    SourceCycleOutcome,
)
from news_bulletin_playlist.engine_runtime import (
    DEFAULT_CONFIG_FILENAME,
    AuthSynchronization,
    OperationalHealthHandler,
    ReloadingEngineCycleRunner,
    _load_runtime_config,
    _operational_status_page,
    _runtime_interval,
    serve,
)
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.runtime import AdminSecurity
from news_bulletin_playlist.spotify.auth import (
    PRODUCTION_SCOPES,
    AuthorizationState,
    SpotifyAuthService,
    SpotifyCredentialStore,
    TokenResponse,
)

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)


class _NeverAuth:
    def __init__(self) -> None:
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        del now
        self.calls += 1
        raise AssertionError("authorization must not be reached for invalid configuration")


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


class _SentinelTokenTransport:
    def __init__(self) -> None:
        self.exchange_calls: list[dict[str, str]] = []

    def exchange_code(
        self,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> TokenResponse:
        assert client_id == "client-id"
        assert redirect_uri == "https://news.example.test/admin/spotify/callback"
        self.exchange_calls.append({"code": code, "verifier": verifier})
        return TokenResponse(
            access_token="access-token-sentinel",
            expires_in=3600,
            granted_scopes=PRODUCTION_SCOPES,
            refresh_token="refresh-token-sentinel",
        )

    def refresh_token(self, *, client_id: str, refresh_token: str) -> TokenResponse:
        raise AssertionError("refresh must not run in callback regression test")


def _serve_one(server: HTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    return thread


def _no_redirect_open(request: urllib.request.Request) -> urllib.error.HTTPError:
    opener = urllib.request.build_opener(_NoRedirect())
    with pytest.raises(urllib.error.HTTPError) as raised:
        opener.open(request, timeout=2)
    return raised.value


def _valid_config() -> str:
    return """schema_version: 1
sources:
  - id: ser
    display_name: Cadena SER
    countries: [ES]
    languages: [es]
    timezone: Europe/Madrid
    enabled: true
    parser_id: ser
    endpoint_url: https://example.test/ser.xml
    external_references:
      - system: spotify
        resource_type: show
        external_id: ser-show
playlists:
  - id: spanish
    display_name: Spanish News
    description: test
    countries: [ES]
    languages: [es]
    enabled: true
    source_selection:
      explicit: [ser]
    destination:
      adapter_id: spotify
      external_id: playlist-id
"""


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    return store


def test_runtime_interval_defaults_to_ten_minutes() -> None:
    assert _runtime_interval({}) == timedelta(minutes=10)
    assert _runtime_interval({"NEWS_PLAYLIST_INTERVAL_SECONDS": "600"}) == timedelta(minutes=10)


def test_runtime_interval_rejects_tight_retry_values() -> None:
    with pytest.raises(RuntimeError, match="at least 60 seconds"):
        _runtime_interval({"NEWS_PLAYLIST_INTERVAL_SECONDS": "59"})
    with pytest.raises(RuntimeError, match="must be an integer"):
        _runtime_interval({"NEWS_PLAYLIST_INTERVAL_SECONDS": "fast"})


def test_runtime_config_is_optional_until_production_file_exists(tmp_path: Path) -> None:
    assert _load_runtime_config(tmp_path, {}) is None
    assert _load_runtime_config(tmp_path, {"NEWS_PLAYLIST_CONFIG": "  "}) is None


def test_runtime_config_loads_default_data_file(tmp_path: Path) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_FILENAME
    config_path.write_text(_valid_config(), encoding="utf-8")

    config = _load_runtime_config(tmp_path, {})

    assert config is not None
    assert [source.id for source in config.sources] == [SourceId("ser")]
    assert [playlist.id for playlist in config.playlists] == [PlaylistId("spanish")]


def test_explicit_missing_runtime_config_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    with pytest.raises(RuntimeError, match="does not exist"):
        _load_runtime_config(tmp_path, {"NEWS_PLAYLIST_CONFIG": str(missing)})


def test_reloading_runner_revalidates_config_each_cycle(tmp_path: Path) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_FILENAME
    config_path.write_text(_valid_config(), encoding="utf-8")
    assert _load_runtime_config(tmp_path, {}) is not None

    auth = _NeverAuth()
    runner = ReloadingEngineCycleRunner(tmp_path, {}, _store(tmp_path), auth)
    config_path.write_text("schema_version: 999\nsources: []\nplaylists: []\n", encoding="utf-8")

    result = runner.run_cycle()

    assert not result.ok
    assert "invalid engine configuration" in (result.error or "")
    assert auth.calls == 0


def test_reloading_runner_fails_closed_if_config_disappears(tmp_path: Path) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_FILENAME
    config_path.write_text(_valid_config(), encoding="utf-8")
    auth = _NeverAuth()
    runner = ReloadingEngineCycleRunner(tmp_path, {}, _store(tmp_path), auth)
    config_path.unlink()

    result = runner.run_cycle()

    assert not result.ok
    assert result.error == "production engine configuration is no longer available"
    assert auth.calls == 0


def test_configured_engine_requires_production_spotify_auth(tmp_path: Path) -> None:
    (tmp_path / DEFAULT_CONFIG_FILENAME).write_text(_valid_config(), encoding="utf-8")

    with pytest.raises(RuntimeError, match="requires production Spotify"):
        serve(data_dir=tmp_path, stop_event=threading.Event(), environ={})


def test_unconfigured_runtime_stops_cleanly_when_requested(tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()

    assert serve(host="127.0.0.1", port=0, data_dir=tmp_path, stop_event=stop, environ={}) == 0


def test_operational_status_page_reports_cycle_without_secret_material() -> None:
    status = OperationalStatus(configured=True)
    cycle = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=12),
        ok=False,
        sources=(
            SourceCycleOutcome(
                source_id=SourceId("ser"),
                collection_ok=False,
                matching_ok=True,
                edition_count=0,
                matched_count=1,
                last_success_at=NOW - timedelta(minutes=10),
                error="source unavailable",
            ),
        ),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spanish"),
                ok=True,
                desired_count=1,
                applied_count=1,
                wrote=False,
                last_success_at=NOW,
            ),
        ),
        error="cycle completed with 1 source failure(s) and 0 playlist failure(s)",
    )
    status.finish_cycle(cycle, next_run_at=NOW + timedelta(minutes=10))

    body = _operational_status_page(
        ready=True,
        spotify_state=AuthorizationState.CONNECTED,
        status=status,
    ).decode("utf-8")

    assert "Connected" in body
    assert "2026-08-30T10:10:00Z" in body
    assert "source unavailable" in body
    assert "0 fetched / 1 matched" in body
    assert "1 desired / 1 verified" in body
    assert "unchanged" in body
    for secret in (
        "access-token-sentinel",
        "refresh-token-sentinel",
        "authorization-code-sentinel",
        "pkce-verifier-sentinel",
    ):
        assert secret not in body


def test_operational_callback_and_status_never_expose_oauth_sentinels(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = _SentinelTokenTransport()
    service = SpotifyAuthService(
        client_id="client-id",
        redirect_uri="https://news.example.test/admin/spotify/callback",
        store=SpotifyCredentialStore(tmp_path / "spotify-auth.json"),
        transport=transport,
    )
    status = OperationalStatus(configured=True)
    previous = (
        OperationalHealthHandler.data_dir,
        OperationalHealthHandler.admin_security,
        OperationalHealthHandler.spotify_auth,
        OperationalHealthHandler.operational_status,
        OperationalHealthHandler.engine_scheduler,
    )
    OperationalHealthHandler.data_dir = tmp_path
    OperationalHealthHandler.admin_security = AdminSecurity("long-enough-admin-password")
    OperationalHealthHandler.spotify_auth = service
    OperationalHealthHandler.operational_status = status
    OperationalHealthHandler.engine_scheduler = None
    server = HTTPServer(("127.0.0.1", 0), OperationalHealthHandler)
    base_url = f"http://127.0.0.1:{server.server_port}"

    authorize_url = service.start_authorization()
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)["state"][0]
    code = "authorization-code-sentinel"
    callback_url = f"{base_url}/admin/spotify/callback?" + urllib.parse.urlencode(
        {"state": state, "code": code}
    )

    try:
        thread = _serve_one(server)
        try:
            successful_callback = _no_redirect_open(urllib.request.Request(callback_url))
            successful_body = successful_callback.read().decode("utf-8")
            successful_headers = str(successful_callback.headers)
        finally:
            thread.join(timeout=2)
        assert successful_callback.code == HTTPStatus.SEE_OTHER

        thread = _serve_one(server)
        try:
            with pytest.raises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(callback_url, timeout=2)
            replay_body = raised.value.read().decode("utf-8")
        finally:
            thread.join(timeout=2)
        assert raised.value.code == HTTPStatus.BAD_REQUEST

        thread = _serve_one(server)
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=2) as response:
                status_body = response.read().decode("utf-8")
        finally:
            thread.join(timeout=2)

        assert len(transport.exchange_calls) == 1
        verifier = transport.exchange_calls[0]["verifier"]
        captured = capsys.readouterr()
        surfaces = (
            successful_body,
            successful_headers,
            replay_body,
            status_body,
            captured.out,
            captured.err,
        )
        for secret in (
            code,
            verifier,
            "access-token-sentinel",
            "refresh-token-sentinel",
        ):
            assert all(secret not in surface for surface in surfaces)
    finally:
        server.server_close()
        (
            OperationalHealthHandler.data_dir,
            OperationalHealthHandler.admin_security,
            OperationalHealthHandler.spotify_auth,
            OperationalHealthHandler.operational_status,
            OperationalHealthHandler.engine_scheduler,
        ) = previous


def test_auth_synchronization_serializes_competing_operations() -> None:
    synchronization = AuthSynchronization()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with synchronization.hold():
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second() -> None:
        assert first_entered.wait(timeout=2)
        with synchronization.hold():
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(timeout=2)
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    assert second_entered.wait(timeout=2)
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()