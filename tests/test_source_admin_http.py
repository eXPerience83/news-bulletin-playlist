from __future__ import annotations

import http.client
import re
import sqlite3
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.admin_session import admin_session_manager_for
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics_runtime import DiagnosticOperationalHealthHandler
from news_bulletin_playlist.engine import OperationalStatus
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.lan_admin import LanAdminSecurity
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_state import (
    ManagedSource,
    ManagedState,
    ManagedStateStore,
    activate_template,
)
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.source_admin import SourceAdminService
from news_bulletin_playlist.spotify.client import SpotifyApiError
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
        items = [
            {
                "id": OTHER,
                "uri": f"spotify:episode:{OTHER}",
                "name": "<img src=x onerror=alert(1)>",
                "release_date": datetime.now(UTC).date().isoformat(),
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
                    "release_date": datetime.now(UTC).date().isoformat(),
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


class _Handler(DiagnosticOperationalHealthHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _server(tmp_path: Path) -> Iterator[tuple[HTTPServer, _Client, ManagedStateStore]]:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    synchronization = ConfigurationSynchronization()
    auth = _Auth()
    client = _Client()
    _Handler.admin_security = LanAdminSecurity(PASSWORD)
    _Handler.data_dir = tmp_path
    _Handler.spotify_auth = None
    _Handler.operational_status = OperationalStatus(configured=False)
    _Handler.engine_lifecycle = None
    _Handler.auth_synchronization = None
    _Handler.configuration_synchronization = synchronization
    _Handler.managed_admin_service = ManagedAdminService(store)
    _Handler.managed_admin_auth = None
    _Handler.source_admin_service = SourceAdminService(
        store,
        synchronization,
        auth,
        SpotifyRateLimitJournal(SQLiteStore(tmp_path / "state.sqlite3")),
        None,
        client_factory=lambda token: client,
    )
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, client, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    server: HTTPServer,
    method: str,
    path: str,
    *,
    cookie: str | None = None,
    form: dict[str, str | list[str]] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {} if cookie is None else {"Cookie": cookie}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form, doseq=True)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def _login(server: HTTPServer) -> str:
    status, headers, _ = _request(
        server, "POST", "/admin/login", form={"username": "admin", "password": PASSWORD}
    )
    assert status == HTTPStatus.SEE_OTHER
    return headers["Set-Cookie"].split(";", 1)[0]


def _csrf(body: bytes) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', body)
    assert match is not None
    return match.group(1).decode()


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


def test_real_http_login_inspect_download_create_logout(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        for path in (
            "/admin/sources/new",
            "/admin/sources/inspection-report.json",
        ):
            assert _request(server, "GET", path)[0] == HTTPStatus.SEE_OTHER
        for path in ("/admin/sources/inspect", "/admin/sources/create"):
            assert _request(server, "POST", path, form={"csrf_token": "bad"})[0] == 303
        cookie = _login(server)
        _, _, initial_page = _request(server, "GET", "/admin/sources/new", cookie=cookie)
        inspect_csrf = _csrf(initial_page)
        status, review = _inspect(
            server,
            cookie,
            f"https://open.spotify.com/show/{SHOW}?si=share",
        )
        assert status == HTTPStatus.OK
        assert _csrf(review) != inspect_csrf
        assert b"provider-sentinel</script>" not in review
        assert b"&lt;script&gt;provider-sentinel&lt;/script&gt;" in review
        assert b"<img src=x onerror" not in review
        assert f"https://open.spotify.com/show/{SHOW}".encode() in review
        assert b"?si=share" not in review
        assert client.calls == ["details", "episodes"]
        assert store.load().sources == ()

        status, headers, report = _request(
            server, "GET", "/admin/sources/inspection-report.json", cookie=cookie
        )
        assert status == HTTPStatus.OK
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert headers["Cache-Control"] == "no-store"
        assert headers["Content-Disposition"] == (
            f'attachment; filename="source-inspection-{SHOW}.json"'
        )
        manager = admin_session_manager_for(_Handler.admin_security)
        context = manager.source_inspection(cookie.partition("=")[2])
        assert context is not None and report == context.report_json
        assert client.calls == ["details", "episodes"]
        for secret in (b"secret-provider-body", b"secret-refresh-token", b"secret-access-token"):
            assert secret not in report + review
        other_cookie = _login(server)
        assert (
            _request(server, "GET", "/admin/sources/inspection-report.json", cookie=other_cookie)[0]
            == HTTPStatus.NOT_FOUND
        )

        form = _create_form(review)
        assert _request(server, "POST", "/admin/sources/create", cookie=cookie, form=form)[0] == 303
        assert client.calls == ["details", "episodes", "details", "episodes"]
        source = ManagedStateStore(store.path).load().sources[0]
        assert isinstance(source, ManagedSource)
        assert (str(source.id), source.spotify_show_id, source.display_name) == (
            f"custom_{SHOW}",
            SHOW,
            "Operator news",
        )
        assert (source.origin.kind.value, source.origin.code) == ("institution", "UN")
        assert (str(source.language), source.editorial_scope.value, source.timezone) == (
            "es-419",
            "INT",
            "Europe/Madrid",
        )
        assert (str(source.parser_id), source.collection_filter_id, source.enabled) == (
            "release_date_title",
            None,
            True,
        )
        with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            diagnostic_count = connection.execute(
                "SELECT COUNT(*) FROM diagnostic_events"
            ).fetchone()
        assert "spotify_first_episode_state" not in tables
        assert "spotify_first_episode_history" not in tables
        assert diagnostic_count == (0,)
        assert (
            _request(server, "GET", "/admin/sources/inspection-report.json", cookie=cookie)[0]
            == 404
        )
        status, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200 and b"Operator news" in sources and b"Built-in" in sources
        assert b"Custom" in sources
        assert (
            _request(
                server,
                "POST",
                "/admin/logout",
                cookie=cookie,
                form={"csrf_token": _csrf(sources)},
            )[0]
            == 303
        )
        assert _request(server, "GET", "/admin/sources", cookie=cookie)[0] == 303


def test_csrf_is_one_use_and_create_requires_review(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        _, _, page = _request(server, "GET", "/admin/sources/new", cookie=cookie)
        token = _csrf(page)
        form = {"csrf_token": token, "locator": SHOW}
        assert (
            _request(server, "POST", "/admin/sources/inspect", cookie=cookie, form=form)[0] == 200
        )
        assert (
            _request(server, "POST", "/admin/sources/inspect", cookie=cookie, form=form)[0] == 403
        )
        assert client.calls == ["details", "episodes"]
        # The consumed inspect token cannot authorize Create either.
        create = _create_form(_request(server, "GET", "/admin/sources/new", cookie=cookie)[2])
        create["csrf_token"] = token
        assert (
            _request(server, "POST", "/admin/sources/create", cookie=cookie, form=create)[0] == 403
        )
        assert client.calls == ["details", "episodes"]
        assert store.load().sources == ()

    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        _, _, page = _request(server, "GET", "/admin/sources/new", cookie=cookie)
        create = _create_form(page)
        assert (
            _request(server, "POST", "/admin/sources/create", cookie=cookie, form=create)[0] == 400
        )
        assert client.calls == [] and store.load().sources == ()


def test_duplicate_and_create_race_fail_before_provider_io(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        builtin_show = BUILTIN_CATALOG.sources[0].external_references[0].external_id
        assert _inspect(server, cookie, builtin_show)[0] == 409
        assert client.calls == []
        _, review = _inspect(server, cookie)
        assert client.calls == ["details", "episodes"]
        service = _Handler.source_admin_service
        assert service is not None
        duplicate = service.validate_new_source(
            show_id=SHOW,
            display_name="Existing",
            origin_kind="country",
            origin_code="ES",
            language="es",
            editorial_scope="NAT",
            timezone="Europe/Madrid",
            parser_id="release_date_title",
            collection_filter_id=None,
        )
        store.save(ManagedState(sources=(duplicate,)))
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/create",
                cookie=cookie,
                form=_create_form(review),
            )[0]
            == 409
        )
        assert client.calls == ["details", "episodes"]
        assert store.load().sources == (duplicate,)


def test_duplicate_inserted_during_fresh_validation_fails_final_locked_check(
    tmp_path: Path,
) -> None:
    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        _, review = _inspect(server, cookie)
        service = _Handler.source_admin_service
        assert service is not None
        competing = service.validate_new_source(
            show_id=SHOW,
            display_name="Competing source",
            origin_kind="country",
            origin_code="ES",
            language="es",
            editorial_scope="NAT",
            timezone="Europe/Madrid",
            parser_id="release_date_title",
            collection_filter_id=None,
        )
        client.on_episodes = lambda: store.save(ManagedState(sources=(competing,)))
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/create",
                cookie=cookie,
                form=_create_form(review),
            )[0]
            == 409
        )
        assert client.calls == ["details", "episodes", "details", "episodes"]
        assert store.load().sources == (competing,)


def test_strict_source_forms_and_missing_csrf_do_not_contact_provider(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        _, _, page = _request(server, "GET", "/admin/sources/new", cookie=cookie)
        token = _csrf(page)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/inspect",
                cookie=cookie,
                form={"csrf_token": "wrong", "locator": SHOW},
            )[0]
            == 403
        )
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/inspect",
                cookie=cookie,
                form={"csrf_token": token, "locator": SHOW, "enabled": "1"},
            )[0]
            == 400
        )
        assert client.calls == [] and store.load().sources == ()


def test_duplicate_form_values_are_rejected_without_provider_io(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        _, _, page = _request(server, "GET", "/admin/sources/new", cookie=cookie)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/inspect",
                cookie=cookie,
                form={"csrf_token": _csrf(page), "locator": [SHOW, OTHER]},
            )[0]
            == 400
        )
        assert client.calls == []
        _, review = _inspect(server, cookie)
        form: dict[str, str | list[str]] = _create_form(review)
        form["spotify_show_id"] = [SHOW, OTHER]
        assert _request(server, "POST", "/admin/sources/create", cookie=cookie, form=form)[0] == 400
        assert client.calls == ["details", "episodes"]
        assert store.load().sources == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spotify_show_id", OTHER),
        ("parser_id", "other"),
        ("collection_filter_id", "arbitrary"),
        ("origin_kind", "wrong"),
        ("language", "en_US"),
        ("editorial_scope", "WRONG"),
        ("timezone", "No/Such_Timezone"),
        ("display_name", " "),
        ("display_name", "x" * 121),
    ],
)
def test_tampered_or_invalid_create_never_calls_provider_again(
    tmp_path: Path, field: str, value: str
) -> None:
    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        status, review = _inspect(server, cookie)
        assert status == 200
        form = _create_form(review)
        form[field] = value
        assert _request(server, "POST", "/admin/sources/create", cookie=cookie, form=form)[0] == 400
        assert client.calls == ["details", "episodes"]
        assert store.load().sources == ()


def test_failed_second_inspect_clears_review_and_failed_fresh_create_preserves_state(
    tmp_path: Path,
) -> None:
    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        _, review = _inspect(server, cookie)
        assert _inspect(server, cookie, "not-a-spotify-show")[0] == 400
        assert (
            _request(server, "GET", "/admin/sources/inspection-report.json", cookie=cookie)[0]
            == 404
        )
        _, _, new_form = _request(server, "GET", "/admin/sources/new", cookie=cookie)
        obsolete_form = _create_form(review)
        obsolete_form["csrf_token"] = _csrf(new_form)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/create",
                cookie=cookie,
                form=obsolete_form,
            )[0]
            == 400
        )
        _, review = _inspect(server, cookie)
        client.error = SpotifyApiError(503, "secret-provider-body")
        status, headers, error = _request(
            server,
            "POST",
            "/admin/sources/create",
            cookie=cookie,
            form=_create_form(review),
        )
        assert status == 503
        assert b"secret-provider-body" not in error
        assert b"secret-provider-body" not in str(headers).encode()
        assert store.load().sources == ()


def test_sources_page_renders_disabled_custom_without_network(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        service = _Handler.source_admin_service
        assert service is not None
        source = service.validate_new_source(
            show_id=SHOW,
            display_name="Disabled news",
            origin_kind="institution",
            origin_code="UN",
            language="es-419",
            editorial_scope="INT",
            timezone="Europe/Madrid",
            parser_id="release_date_title",
            collection_filter_id=None,
        )
        playlist = replace(
            activate_template(BUILTIN_CATALOG.playlist("spain_spanish_news"), "destination"),
            source_ids=(source.id,),
            display_name="Playlist using disabled news",
        )
        store.save(ManagedState(sources=(replace(source, enabled=False),), playlists=(playlist,)))
        cookie = _login(server)
        status, _, body = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        assert b"Disabled news" in body and b"Disabled" in body
        assert b"Built-in" in body and b"Custom" in body
        assert b"Playlist using disabled news" in body
        assert f"https://open.spotify.com/show/{SHOW}".encode() in body
        assert client.calls == []


def test_review_surfaces_day_order_ambiguity_without_rejecting_candidate(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        client.two_episodes = True
        cookie = _login(server)
        status, review = _inspect(server, cookie)
        assert status == 200
        assert b"same_day_order_ambiguous" in review
        assert b"ambiguous" in review
        assert b"15_to_30m: 1" in review
        assert store.load().sources == ()


def test_web_intake_uses_durable_global_cooldown_without_retry(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        _, review = _inspect(server, cookie)
        client.error = SpotifyApiError(429, "secret-provider-body", retry_after=60)
        status, _, error = _request(
            server,
            "POST",
            "/admin/sources/create",
            cookie=cookie,
            form=_create_form(review),
        )
        assert status == 503 and b"secret-provider-body" not in error
        assert client.calls == ["details", "episodes", "details"]
        service = _Handler.source_admin_service
        assert service is not None and service.rate_limit.get() is not None
        assert _inspect(server, cookie)[0] == 503
        assert client.calls == ["details", "episodes", "details"]
        assert store.load().sources == ()


def test_operator_display_name_is_escaped_after_create(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, _client, store):
        cookie = _login(server)
        _, review = _inspect(server, cookie)
        form = _create_form(review)
        form["display_name"] = "<img src=x onerror=alert(1)>"
        assert _request(server, "POST", "/admin/sources/create", cookie=cookie, form=form)[0] == 303
        _, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert b"<img src=x onerror" not in page
        assert b"&lt;img src=x onerror=alert(1)&gt;" in page
        assert store.load().sources[0].display_name == form["display_name"]
