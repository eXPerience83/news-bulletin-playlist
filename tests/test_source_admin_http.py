from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest
from _html import assert_valid_admin_html

from news_bulletin_playlist import engine_runtime
from news_bulletin_playlist.admin_session import admin_session_manager_for
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.diagnostics_runtime import DiagnosticOperationalHealthHandler
from news_bulletin_playlist.engine import OperationalStatus
from news_bulletin_playlist.engine_cycle_synchronization import EngineCycleSynchronization
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.lan_admin import LanAdminSecurity
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_provisioning import ProvisioningIntent, ProvisioningState
from news_bulletin_playlist.managed_state import (
    ManagedSource,
    ManagedState,
    ManagedStateStore,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.models import (
    CountryCode,
    EditorialScope,
    LanguageTag,
    ParserId,
    SourceId,
    SourceOrigin,
    TemporalStrategy,
)
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.source_admin import SourceAdminService
from news_bulletin_playlist.spotify.auth import SpotifyAuthService
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError
from news_bulletin_playlist.spotify.first_models import (
    RefreshReason,
    SpotifyFirstEpisode,
    TimestampSource,
)
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
    _Handler.admin_security = LanAdminSecurity(PASSWORD)
    _Handler.data_dir = tmp_path
    _Handler.spotify_auth = None
    _Handler.operational_status = OperationalStatus(configured=False)
    _Handler.engine_lifecycle = lifecycle or _Lifecycle()  # type: ignore[assignment]
    _Handler.auth_synchronization = None
    _Handler.configuration_synchronization = synchronization
    _Handler.managed_admin_service = ManagedAdminService(store)
    _Handler.managed_admin_auth = None
    diagnostic_store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite3")
    diagnostic_store.initialize()
    _Handler.diagnostic_store = diagnostic_store
    _Handler.operational_diagnostics = None
    runtime_store = SQLiteStore(tmp_path / "state.sqlite3")
    runtime_store.initialize()
    first_journal = SpotifyFirstJournal(runtime_store)
    first_journal.initialize()
    detector_journal = PublicDetectorJournal(runtime_store)
    detector_journal.initialize()
    cycle_synchronization = EngineCycleSynchronization()
    _Handler.source_admin_service = SourceAdminService(
        store,
        synchronization,
        auth,
        SpotifyRateLimitJournal(runtime_store),
        None,
        client_factory=lambda token: client,
        first_journal=first_journal,
        detector_journal=detector_journal,
        cycle_synchronization=cycle_synchronization,
        provisioning_journal=_Handler.managed_admin_service.provisioning_journal,
        public_client=_PublicAudit(),
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


def _configured_source(store: ManagedStateStore, *, enabled: bool = True) -> ManagedSource:
    source = _Handler.source_admin_service.validate_new_source(  # type: ignore[union-attr]
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


def test_source_admin_rendered_html_contract_over_real_http(tmp_path: Path) -> None:
    """Keep the six normal Source Admin full-page surfaces visible in one matrix."""
    with _server(tmp_path) as (server, _client, store):
        cookie = _login(server)

        # Add Source and successful inspection/review, with the rendered one-use CSRF.
        status, _, page = _request(server, "GET", "/admin/sources/new", cookie=cookie)
        assert status == HTTPStatus.OK
        assert_valid_admin_html(page.decode())
        status, _, review = _request(
            server,
            "POST",
            "/admin/sources/inspect",
            cookie=cookie,
            form={"csrf_token": _csrf(page), "locator": SHOW},
        )
        assert status == HTTPStatus.OK
        assert_valid_admin_html(review.decode())

        # Read-only configured Source Audit, using the existing real CSRF journey.
        source = _configured_source(store)
        status, audit = _audit(server, cookie, str(source.id))
        assert status == HTTPStatus.OK
        assert_valid_admin_html(audit.decode())

        # Custom edit and built-in public-release edit are distinct full documents.
        status, _, custom_edit = _request(
            server, "GET", f"/admin/sources/edit?source_id={source.id}", cookie=cookie
        )
        assert status == HTTPStatus.OK
        assert_valid_admin_html(custom_edit.decode())
        status, _, builtin_edit = _request(
            server, "GET", "/admin/sources/edit?source_id=rne", cookie=cookie
        )
        assert status == HTTPStatus.OK
        assert_valid_admin_html(builtin_edit.decode())

        # Unreferenced custom source: audit the confirmation with its deletion POST form.
        store.save(replace(store.load(), playlists=()))
        status, _, confirmation = _request(
            server, "GET", f"/admin/sources/delete?source_id={source.id}", cookie=cookie
        )
        assert status == HTTPStatus.OK
        assert b'<form method="post" action="/admin/sources/delete">' in confirmation
        assert_valid_admin_html(confirmation.decode())


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


@pytest.mark.parametrize("strategy", list(TemporalStrategy))
def test_create_and_edit_temporal_strategy_over_real_http(tmp_path, strategy):
    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        status, review = _inspect(server, cookie)
        assert status == 200
        assert b'<option value="auto" selected>Auto only (recommended)</option>' in review
        assert b'<select name="parser_id">' not in review
        for option in TemporalStrategy:
            assert f'<option value="{option.value}"'.encode() in review
        form = {**_create_form(review), "temporal_strategy": strategy.value}
        assert _request(server, "POST", "/admin/sources/create", cookie=cookie, form=form)[0] == 303
        assert store.load().sources[0].temporal_strategy is strategy
        path = f"/admin/sources/edit?source_id=custom_{SHOW}"
        status, _, edit = _request(server, "GET", path, cookie=cookie)
        assert status == 200
        assert f'<option value="{strategy.value}" selected>'.encode() in edit
        calls = list(client.calls)
        changed = (
            TemporalStrategy.DISABLED
            if strategy is not TemporalStrategy.DISABLED
            else TemporalStrategy.AUTO
        )
        update = {**_update_form(_csrf(edit)), "temporal_strategy": changed.value}
        assert (
            _request(server, "POST", "/admin/sources/update", cookie=cookie, form=update)[0] == 303
        )
        assert store.load().sources[0].temporal_strategy is changed
        assert client.calls == calls


@pytest.mark.parametrize("action", ["create", "update"])
def test_temporal_strategy_unknown_value_is_rejected_without_provider_io(tmp_path, action):
    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        if action == "create":
            _, review = _inspect(server, cookie)
            form = _create_form(review)
        else:
            _configured_source(store)
            _, _, edit = _request(
                server, "GET", f"/admin/sources/edit?source_id=custom_{SHOW}", cookie=cookie
            )
            form = _update_form(_csrf(edit))
        previous = store.load()
        calls = list(client.calls)
        form["temporal_strategy"] = "arbitrary-regex-or-provider"
        assert (
            _request(server, "POST", f"/admin/sources/{action}", cookie=cookie, form=form)[0] == 400
        )
        assert store.load() == previous and client.calls == calls


def _lifecycle_form(token: str, *, enabled: str) -> dict[str, str]:
    return {"csrf_token": token, "source_id": f"custom_{SHOW}", "enabled": enabled}


def _playlist_form(token: str, source_ids: list[str]) -> dict[str, str | list[str]]:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    return {
        "csrf_token": token,
        "playlist_id": str(template.id),
        "display_name": "Operator playlist",
        "description": "",
        "cover_id": template.cover_id,
        "source_id": source_ids,
        "enabled": "1",
        "max_duration_minutes": "30",
        "max_duration_seconds_remainder": "0",
    }


def test_real_http_assignment_unassignment_and_guarded_delete(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        service = _Handler.source_admin_service
        assert service is not None
        source = _configured_source(store)
        original = store.load()
        updated = replace(original.playlists[0], source_ids=("ser",))
        store.save(replace(original, playlists=(updated,)))
        for path in ("/admin/sources/delete?source_id=" + str(source.id),):
            assert _request(server, "GET", path)[0] == 303
        anonymous_delete = _request(
            server, "POST", "/admin/sources/delete", form={"source_id": str(source.id)}
        )
        assert anonymous_delete[0] == 303

        cookie = _login(server)
        _, _, playlists = _request(server, "GET", "/admin/", cookie=cookie)
        control = f'type="checkbox" name="source_id" value="{source.id}"'.encode()
        assert control in playlists and b"Custom" in playlists
        assert b'type="hidden" name="source_id" value="custom_' not in playlists
        assert (
            _request(
                server,
                "POST",
                "/admin/playlists/update",
                cookie=cookie,
                form=_playlist_form(_csrf(playlists), ["ser", str(source.id)]),
            )[0]
            == 303
        )
        assert store.load().playlists[0].source_ids == ("ser", source.id)
        assert client.calls == [] and service.auth.calls == 0  # type: ignore[attr-defined]
        _, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert b"Operator playlist" in sources and b"Unassign before deletion" in sources
        custom_row = next(
            row
            for row in re.findall(rb"<tr>.*?</tr>", sources, flags=re.DOTALL)
            if control.split(b'value="')[1].split(b'"')[0] in row
        )
        assert b'<td class="source-actions-cell"><div class="source-actions">' in custom_row
        disclosure = re.search(
            rb'<details class="source-action-disclosure">(.*?)</details>',
            custom_row,
            flags=re.DOTALL,
        )
        assert disclosure is not None
        assert b"<summary>Disable impact</summary>" in disclosure[1]
        assert b"Disable affects: Operator playlist" in disclosure[1]
        assert b'<p class="warning">Disable affects:' not in custom_row
        assert b'<form method="post" action="/admin/sources/set-enabled">' in custom_row
        assert b">Disable</button>" in custom_row
        assert b"its episodes may be removed from Spotify" in disclosure[1]
        assert b"Assignments are kept" in disclosure[1]
        assert_valid_admin_html(sources.decode())
        assert b"next reconciliation" in disclosure[1]
        assert b"continue updating with their remaining enabled sources" in disclosure[1]
        assert b"return automatically on Enable" in disclosure[1]
        assert b"pauses affected destination updates" not in custom_row
        assert custom_row.index(b"</details>") < custom_row.index(b">Disable</button>")
        for row in re.findall(rb"<tr>.*?</tr>", sources, flags=re.DOTALL):
            if b"Built-in" in row:
                assert b"/admin/sources/delete" not in row
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/set-enabled",
                cookie=cookie,
                form=_lifecycle_form(_csrf(sources), enabled="0"),
            )[0]
            == 303
        )
        _, _, playlists = _request(server, "GET", "/admin/", cookie=cookie)
        assert control + b" checked" in playlists
        assert b"Inactive assignments" in playlists
        normal_sources = playlists.split(b"<legend>Sources</legend>", 1)[1].split(
            b"</fieldset>", 1
        )[0]
        inactive = playlists.split(b"<legend>Inactive assignments</legend>", 1)[1].split(
            b"</fieldset>", 1
        )[0]
        assert control not in normal_sources
        assert b'value="ser" checked' in normal_sources
        assert control + b" checked" in inactive
        assert b"Disabled" in inactive
        assert b"uncheck to permanently unassign" in inactive
        assert b"return automatically" in playlists
        assert b'type="hidden" name="source_id" value="custom_' not in playlists
        delete_path = f"/admin/sources/delete?source_id={source.id}"
        assert b"Unassign this source" in _request(server, "GET", delete_path, cookie=cookie)[2]
        _, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/delete",
                cookie=cookie,
                form={"csrf_token": _csrf(sources), "source_id": str(source.id)},
            )[0]
            == 409
        )
        assert store.load().sources == (replace(source, enabled=False),)
        events = _Handler.diagnostic_store.list_events(source_id=str(source.id))
        assert not any(event.event_name == "admin_source_deleted" for event in events)

        _, _, playlists = _request(server, "GET", "/admin/", cookie=cookie)
        assert (
            _request(
                server,
                "POST",
                "/admin/playlists/update",
                cookie=cookie,
                form=_playlist_form(_csrf(playlists), ["ser"]),
            )[0]
            == 303
        )
        assert store.load().sources == (replace(source, enabled=False),)
        assert store.load().playlists[0].source_ids == ("ser",)
        _, _, confirm = _request(server, "GET", delete_path, cookie=cookie)
        assert b"Delete custom source" in confirm and b"30-day" in confirm
        token = _csrf(confirm)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/delete",
                cookie=cookie,
                form={"csrf_token": "invalid", "source_id": str(source.id)},
            )[0]
            == 403
        )
        status, headers, _ = _request(
            server,
            "POST",
            "/admin/sources/delete",
            cookie=cookie,
            form={"csrf_token": token, "source_id": str(source.id)},
        )
        assert status == 303 and headers["Location"] == "/admin/sources?notice=source-deleted"
        assert store.load().sources == ()
        assert store.load().playlists[0].source_ids == ("ser",)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/delete",
                cookie=cookie,
                form={"csrf_token": token, "source_id": str(source.id)},
            )[0]
            == 403
        )
        assert client.calls == [] and service.auth.calls == 0  # type: ignore[attr-defined]
        events = tuple(
            event
            for event in _Handler.diagnostic_store.list_events(source_id=str(source.id))
            if event.event_name == "admin_source_deleted"
        )
        assert len(events) == 1 and events[0].source_id == str(source.id)
        assert events[0].details == {
            "operation": "source_configuration",
            "outcome": "applied",
            "next_state": "deleted",
        }
        assert "secret" not in str(events[0].details)
        _, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert str(source.id).encode() not in sources
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
        assert _request(server, "GET", delete_path, cookie=cookie)[0] == 303


def test_blocked_source_disable_has_one_contextual_disclosure(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        state = store.load()
        playlist_name = "Operator <news> & updates"
        store.save(
            replace(state, playlists=(replace(state.playlists[0], display_name=playlist_name),))
        )
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        row = next(
            row
            for row in re.findall(rb"<tr>.*?</tr>", page, flags=re.DOTALL)
            if str(source.id).encode() in row
        )
        disclosures = re.findall(
            rb'<details class="source-action-disclosure">(.*?)</details>', row, flags=re.DOTALL
        )
        assert len(disclosures) == 1
        assert b"<summary>Cannot disable</summary>" in disclosures[0]
        assert b"Operator &lt;news&gt; &amp; updates" in disclosures[0]
        assert b"would have no enabled sources" in disclosures[0]
        assert b"Add another enabled source or pause the active playlists first" in disclosures[0]
        assert b"resolve any pending activation" in disclosures[0]
        assert b"Disable affects:" not in row
        assert b"Disable impact" not in row
        assert b">Disable</button>" not in row
        assert b"/admin/sources/set-enabled" not in row
        assert_valid_admin_html(page.decode())
        assert client.calls == [] and _Handler.source_admin_service.auth.calls == 0


@pytest.mark.parametrize("enabled", [True, False])
def test_unreferenced_or_disabled_source_actions_need_no_disclosure(
    tmp_path: Path, enabled: bool
) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store, enabled=enabled)
        if enabled:
            store.save(replace(store.load(), playlists=()))
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        row = next(
            row
            for row in re.findall(rb"<tr>.*?</tr>", page, flags=re.DOTALL)
            if str(source.id).encode() in row
        )
        assert b"<details" not in row
        assert b"Disable affects:" not in row
        assert b"Cannot disable:" not in row
        assert b'<form method="post" action="/admin/sources/set-enabled">' in row
        assert (b">Disable</button>" if enabled else b">Enable</button>") in row
        assert_valid_admin_html(page.decode())
        assert client.calls == [] and _Handler.source_admin_service.auth.calls == 0


def test_source_action_css_wraps_and_bounds_disclosure_content(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, _, _):
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        css = page.split(b"<style>", 1)[1].split(b"</style>", 1)[0]
        cell = re.search(rb"td\.source-actions-cell\s*\{([^}]+)\}", css)
        assert cell is not None and b"white-space: normal" in cell[1]
        prose = re.search(rb"\.source-action-disclosure p\s*\{([^}]+)\}", css)
        assert prose is not None
        assert b"white-space: normal" in prose[1]
        assert b"max-width: 100%" in prose[1] and b"width: 24rem" in prose[1]
        focus = re.search(rb"\.source-action-disclosure summary:focus-visible\s*\{([^}]+)\}", css)
        assert focus is not None and b"outline: 3px solid" in focus[1]
        assert b"overflow-x: auto" in css


def test_disabled_unselected_source_is_not_offered_and_backend_rejects_manual_assignment(
    tmp_path: Path,
) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store, enabled=False)
        current = store.load()
        playlist = replace(current.playlists[0], source_ids=("ser",))
        store.save(replace(current, playlists=(playlist,)))
        cookie = _login(server)
        _, _, page = _request(server, "GET", "/admin/", cookie=cookie)
        assert f'type="checkbox" name="source_id" value="{source.id}"'.encode() not in page
        status = _request(
            server,
            "POST",
            "/admin/playlists/update",
            cookie=cookie,
            form=_playlist_form(_csrf(page), ["ser", str(source.id)]),
        )[0]
        assert status == 409
        assert store.load().playlists[0].source_ids == ("ser",)
        assert client.calls == []


def test_delete_rejects_builtin_bad_form_and_busy_cycle_over_http(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        current = store.load()
        store.save(replace(current, playlists=()))
        service = _Handler.source_admin_service
        assert service is not None and service.cycle_synchronization is not None
        cookie = _login(server)
        builtin_delete = _request(
            server, "GET", "/admin/sources/delete?source_id=ser", cookie=cookie
        )
        assert builtin_delete[0] == 404
        assert (
            _request(
                server,
                "GET",
                f"/admin/sources/delete?source_id={source.id}&source_id=ser",
                cookie=cookie,
            )[0]
            == 400
        )
        _, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/delete",
                cookie=cookie,
                form={"csrf_token": _csrf(sources), "source_id": "ser"},
            )[0]
            == 409
        )
        _, _, confirm = _request(
            server, "GET", f"/admin/sources/delete?source_id={source.id}", cookie=cookie
        )
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/delete",
                cookie=cookie,
                form={"csrf_token": _csrf(confirm), "source_id": str(source.id), "enabled": "0"},
            )[0]
            == 400
        )
        _, _, confirm = _request(
            server, "GET", f"/admin/sources/delete?source_id={source.id}", cookie=cookie
        )
        with service.cycle_synchronization.hold_cycle():
            assert (
                _request(
                    server,
                    "POST",
                    "/admin/sources/delete",
                    cookie=cookie,
                    form={"csrf_token": _csrf(confirm), "source_id": str(source.id)},
                )[0]
                == 409
            )
        assert store.load().sources == (source,)
        events = _Handler.diagnostic_store.list_events(source_id=str(source.id))
        assert not any(event.event_name == "admin_source_deleted" for event in events)
        _, _, confirm = _request(
            server, "GET", f"/admin/sources/delete?source_id={source.id}", cookie=cookie
        )
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/delete",
                cookie=cookie,
                form={"csrf_token": _csrf(confirm), "source_id": str(source.id)},
            )[0]
            == 303
        )
        assert store.load().sources == () and client.calls == []


def test_stale_playlist_form_cannot_assign_source_deleted_after_render(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        current = store.load()
        playlist = replace(current.playlists[0], source_ids=("ser",))
        store.save(replace(current, playlists=(playlist,)))
        cookie = _login(server)
        _, _, old_form = _request(server, "GET", "/admin/", cookie=cookie)
        service = _Handler.source_admin_service
        assert service is not None
        assert service.delete(str(source.id)) is True
        assert (
            _request(
                server,
                "POST",
                "/admin/playlists/update",
                cookie=cookie,
                form=_playlist_form(_csrf(old_form), ["ser", str(source.id)]),
            )[0]
            == 409
        )
        assert store.load().playlists[0].source_ids == ("ser",)
        assert client.calls == []


def test_selected_disabled_source_cannot_be_removed_from_active_playlist_to_empty(
    tmp_path: Path,
) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store, enabled=False)
        cookie = _login(server)
        _, _, page = _request(server, "GET", "/admin/", cookie=cookie)
        assert f'value="{source.id}" checked'.encode() in page
        assert (
            _request(
                server,
                "POST",
                "/admin/playlists/update",
                cookie=cookie,
                form=_playlist_form(_csrf(page), []),
            )[0]
            == 409
        )
        assert store.load().playlists[0].source_ids == (source.id,)
        _, _, page = _request(server, "GET", "/admin/", cookie=cookie)
        pause_form = _playlist_form(_csrf(page), [])
        del pause_form["enabled"]
        assert (
            _request(server, "POST", "/admin/playlists/update", cookie=cookie, form=pause_form)[0]
            == 303
        )
        assert store.load().playlists[0].source_ids == ()
        assert store.load().sources[0] == source
        assert client.calls == []


def test_delete_confirmation_escapes_name_and_rejects_duplicate_post_field(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        hostile = replace(source, display_name="<img src=x onerror=alert(1)>")
        store.save(ManagedState(sources=(hostile,)))
        cookie = _login(server)
        path = f"/admin/sources/delete?source_id={source.id}"
        status, headers, confirm = _request(server, "GET", path, cookie=cookie)
        assert status == 200 and headers["Cache-Control"] == "no-store"
        assert b"<img src=x onerror" not in confirm
        assert b"&lt;img src=x onerror=alert(1)&gt;" in confirm
        token = _csrf(confirm)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/delete",
                cookie=cookie,
                form={"csrf_token": token, "source_id": [str(source.id), "ser"]},
            )[0]
            == 400
        )
        assert store.load().sources == (hostile,)
        assert client.calls == []


def test_failed_runtime_cleanup_emits_no_success_event_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        store.save(ManagedState(sources=(source,)))
        service = _Handler.source_admin_service
        assert service is not None and service.detector_journal is not None
        original = service.detector_journal.clear_source_state

        def fail(source_id: object) -> None:
            del source_id
            raise PersistenceError("secret-sqlite-path")

        monkeypatch.setattr(service.detector_journal, "clear_source_state", fail)
        cookie = _login(server)
        path = f"/admin/sources/delete?source_id={source.id}"
        _, _, confirm = _request(server, "GET", path, cookie=cookie)
        status, _, body = _request(
            server,
            "POST",
            "/admin/sources/delete",
            cookie=cookie,
            form={"csrf_token": _csrf(confirm), "source_id": str(source.id)},
        )
        assert status == 500 and b"secret-sqlite-path" not in body
        assert store.load().sources == (source,)
        events = _Handler.diagnostic_store.list_events(source_id=str(source.id))
        assert not any(event.event_name == "admin_source_deleted" for event in events)
        monkeypatch.setattr(service.detector_journal, "clear_source_state", original)
        _, _, confirm = _request(server, "GET", path, cookie=cookie)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/delete",
                cookie=cookie,
                form={"csrf_token": _csrf(confirm), "source_id": str(source.id)},
            )[0]
            == 303
        )
        assert store.load().sources == () and client.calls == []


def test_http_delete_then_fresh_create_same_show_cannot_reuse_old_runtime_state(
    tmp_path: Path,
) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        store.save(ManagedState(sources=(source,)))
        service = _Handler.source_admin_service
        assert service is not None
        first = service.first_journal
        detector = service.detector_journal
        assert first is not None and detector is not None
        now = datetime.now(UTC)
        episode = SpotifyFirstEpisode(
            source_id=source.id,
            spotify_show_id=SHOW,
            spotify_episode_id=OTHER,
            spotify_episode_uri=f"spotify:episode:{OTHER}",
            title="Old accepted bulletin",
            edition_at=None,
            timestamp_source=TimestampSource.FIRST_SEEN,
            release_date_raw=now.date().isoformat(),
            release_date_precision="day",
            duration_seconds=305,
            first_seen_at=now,
            last_seen_at=now,
        )
        first.replace_source_episodes(source.id, (episode,), coverage_proven=True)
        definition = compile_engine_config(BUILTIN_CATALOG, store.load()).sources[-1]
        first.record_refresh(
            definition,
            refreshed_at=now,
            reason=RefreshReason.BOOTSTRAP,
            detector_episode_id=OTHER,
            api_episode_id=OTHER,
            request_count=1,
            complete=True,
            coverage_proven=True,
            coverage_cutoff_at=now - timedelta(hours=48),
        )
        detector.record_success(
            PublicEpisodeObservation(
                source_id=source.id,
                spotify_show_id=SHOW,
                episode_id=OTHER,
                episode_uri=f"spotify:episode:{OTHER}",
                name="Old accepted bulletin",
                title="Old accepted bulletin",
                release_date_raw=now.date().isoformat(),
                duration_ms=305_000,
                related_entity_uri=f"spotify:show:{SHOW}",
                observed_at=now,
            )
        )
        cookie = _login(server)
        _, _, confirm = _request(
            server, "GET", f"/admin/sources/delete?source_id={source.id}", cookie=cookie
        )
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/delete",
                cookie=cookie,
                form={"csrf_token": _csrf(confirm), "source_id": str(source.id)},
            )[0]
            == 303
        )
        assert first.get_source_state(source.id) is None
        assert first.list_episodes(source_ids=(source.id,)) == ()
        assert detector.get(source.id, SHOW) is None
        history = first.list_episode_history(
            observed_since=now - timedelta(days=1),
            observed_until=now + timedelta(days=1),
            source_id=source.id,
        )
        assert len(history) == 1 and history[0].duration_seconds == 305
        status, review = _inspect(server, cookie)
        assert status == 200
        assert (
            _request(
                server, "POST", "/admin/sources/create", cookie=cookie, form=_create_form(review)
            )[0]
            == 303
        )
        assert store.load().sources[0].id == source.id
        assert first.get_source_state(source.id) is None
        assert first.list_episodes(source_ids=(source.id,)) == ()
        assert detector.get(source.id, SHOW) is None
        assert (
            first.list_episode_history(
                observed_since=now - timedelta(days=1),
                observed_until=now + timedelta(days=1),
                source_id=source.id,
            )
            == history
        )
        assert client.calls == ["details", "episodes", "details", "episodes"]


def test_real_http_custom_lifecycle_is_local_and_preserves_references(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        original = _configured_source(store)
        current = store.load()
        store.save(
            replace(
                current, playlists=(replace(current.playlists[0], source_ids=("ser", original.id)),)
            )
        )
        auth = _Handler.source_admin_service.auth  # type: ignore[union-attr]
        for path in ("/admin/sources/edit?source_id=custom_" + SHOW,):
            assert _request(server, "GET", path)[0] == 303
        for path in ("/admin/sources/update", "/admin/sources/set-enabled"):
            assert _request(server, "POST", path, form={"csrf_token": "bad"})[0] == 303
        cookie = _login(server)
        _, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert b"Edit" in sources and b"Disable" in sources
        for row in re.findall(rb"<tr>.*?</tr>", sources, flags=re.DOTALL):
            if b"Built-in" in row:
                assert b"Edit public release time" in row
                assert b"/admin/sources/set-enabled" not in row
        edit_path = f"/admin/sources/edit?source_id=custom_{SHOW}"
        status, _, edit = _request(server, "GET", edit_path, cookie=cookie)
        assert status == 200 and b"Europe/Madrid" in edit and b"release_date_title" in edit
        assert b'name="timezone"' not in edit and b'name="parser_id"' not in edit
        token = _csrf(edit)
        status, headers, _ = _request(
            server, "POST", "/admin/sources/update", cookie=cookie, form=_update_form(token)
        )
        assert status == 303 and headers["Location"] == "/admin/sources?notice=source-updated"
        assert (
            _request(
                server, "POST", "/admin/sources/update", cookie=cookie, form=_update_form(token)
            )[0]
            == 403
        )
        edited = ManagedStateStore(store.path).load().sources[0]
        assert edited == replace(
            original,
            display_name="Revised news",
            origin=SourceOrigin.institution("UN"),
            language="es-419",
            editorial_scope="INT",
        )
        _, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert b"Revised news" in sources
        token = _csrf(sources)
        status, headers, _ = _request(
            server,
            "POST",
            "/admin/sources/set-enabled",
            cookie=cookie,
            form=_lifecycle_form(token, enabled="0"),
        )
        assert status == 303 and headers["Location"] == "/admin/sources?notice=source-disabled"
        disabled_state = ManagedStateStore(store.path).load()
        assert disabled_state.sources == (replace(edited, enabled=False),)
        assert disabled_state.playlists[0].source_ids == ("ser", original.id)
        _, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert b"Disabled" in sources and b"Enable" in sources
        _, _, playlists = _request(server, "GET", "/admin/", cookie=cookie)
        assert b"Revised news" in playlists and b"Inactive assignments" in playlists
        assert f'name="source_id" value="custom_{SHOW}"'.encode() in playlists
        selected_control = f'type="checkbox" name="source_id" value="custom_{SHOW}" checked'
        assert selected_control.encode() in playlists
        assert f'type="hidden" name="source_id" value="custom_{SHOW}"'.encode() not in playlists
        token = _csrf(sources)
        status, headers, _ = _request(
            server,
            "POST",
            "/admin/sources/set-enabled",
            cookie=cookie,
            form=_lifecycle_form(token, enabled="1"),
        )
        assert status == 303 and headers["Location"] == "/admin/sources?notice=source-enabled"
        assert ManagedStateStore(store.path).load().sources == (edited,)
        assert client.calls == [] and auth.calls == 0
        events = _Handler.diagnostic_store.list_events(source_id=str(original.id))
        assert {event.event_name for event in events} == {
            "admin_source_updated",
            "admin_source_disabled",
            "admin_source_enabled",
        }
        assert all(event.details["outcome"] == "applied" for event in events)
        assert all(event.details["operation"] == "source_configuration" for event in events)
        assert all("Revised" not in str(event.details) for event in events)
        _, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert (
            _request(
                server, "POST", "/admin/logout", cookie=cookie, form={"csrf_token": _csrf(sources)}
            )[0]
            == 303
        )
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/set-enabled",
                cookie=cookie,
                form=_lifecycle_form(token, enabled="0"),
            )[0]
            == 303
        )


@pytest.mark.parametrize(
    ("extra", "value"),
    [
        ("spotify_show_id", OTHER),
        ("timezone", "UTC"),
        ("parser_id", "other"),
        ("collection_filter_id", "other"),
        ("id", "custom_other"),
        ("enabled", "0"),
    ],
)
def test_update_rejects_immutable_post_fields(tmp_path: Path, extra: str, value: str) -> None:
    with _server(tmp_path) as (server, client, store):
        original = _configured_source(store)
        cookie = _login(server)
        _, _, edit = _request(
            server, "GET", f"/admin/sources/edit?source_id={original.id}", cookie=cookie
        )
        form = _update_form(_csrf(edit))
        form[extra] = value
        assert _request(server, "POST", "/admin/sources/update", cookie=cookie, form=form)[0] == 400
        assert store.load().sources == (original,)
        assert client.calls == []
        assert _Handler.diagnostic_store.list_events(source_id=str(original.id)) == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("display_name", " "),
        ("display_name", "X" * 121),
        ("display_name", "line\nbreak"),
        ("origin_kind", "other"),
        ("origin_code", "UN"),
        ("language", "es_US"),
        ("editorial_scope", "OTHER"),
    ],
)
def test_update_rejects_invalid_metadata_without_provider_call(
    tmp_path: Path, field: str, value: str
) -> None:
    with _server(tmp_path) as (server, client, store):
        original = _configured_source(store)
        cookie = _login(server)
        _, _, edit = _request(
            server, "GET", f"/admin/sources/edit?source_id={original.id}", cookie=cookie
        )
        form = _update_form(_csrf(edit))
        if field == "origin_code":
            form["origin_kind"] = "country"
        form[field] = value
        assert _request(server, "POST", "/admin/sources/update", cookie=cookie, form=form)[0] == 400
        assert store.load().sources == (original,)
        assert client.calls == []


def test_lifecycle_forms_reject_unknown_fields_duplicate_values_and_bad_boolean(
    tmp_path: Path,
) -> None:
    with _server(tmp_path) as (server, client, store):
        original = _configured_source(store)
        cookie = _login(server)
        for modified in (
            {"timezone": "UTC"},
            {"enabled": "true"},
            {"source_id": [str(original.id), str(original.id)]},
        ):
            _, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
            form: dict[str, str | list[str]] = _lifecycle_form(_csrf(page), enabled="0")
            form.update(modified)
            assert (
                _request(server, "POST", "/admin/sources/set-enabled", cookie=cookie, form=form)[0]
                == 400
            )
        assert store.load().sources == (original,)
        assert client.calls == []
        assert _Handler.diagnostic_store.list_events() == ()


def test_builtin_lifecycle_rejected_and_invalid_csrf_has_no_effect(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        _configured_source(store)
        cookie = _login(server)
        builtin = str(BUILTIN_CATALOG.sources[0].id)
        assert (
            _request(server, "GET", f"/admin/sources/edit?source_id={builtin}", cookie=cookie)[0]
            == 200
        )
        _, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        token = _csrf(page)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/set-enabled",
                cookie=cookie,
                form=_lifecycle_form("bad", enabled="0"),
            )[0]
            == 403
        )
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/update",
                cookie=cookie,
                form={**_update_form(token), "source_id": builtin},
            )[0]
            == 409
        )
        _, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/set-enabled",
                cookie=cookie,
                form={**_lifecycle_form(_csrf(page), enabled="0"), "source_id": builtin},
            )[0]
            == 409
        )
        assert len(store.load().sources) == 1 and client.calls == []
        assert _Handler.diagnostic_store.list_events() == ()


def test_lifecycle_reconciles_active_playlists_but_not_sources_alone(tmp_path: Path) -> None:
    lifecycle = _Lifecycle()
    with _server(tmp_path, lifecycle=lifecycle) as (server, client, store):
        source = _configured_source(store)
        current = store.load()
        store.save(
            replace(
                current, playlists=(replace(current.playlists[0], source_ids=("ser", source.id)),)
            )
        )
        cookie = _login(server)
        _, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/set-enabled",
                cookie=cookie,
                form=_lifecycle_form(_csrf(page), enabled="0"),
            )[0]
            == 303
        )
        assert lifecycle.reconcile_calls == [True]
        state = store.load()
        store.save(replace(state, playlists=()))
        _, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/set-enabled",
                cookie=cookie,
                form=_lifecycle_form(_csrf(page), enabled="1"),
            )[0]
            == 303
        )
        assert lifecycle.reconcile_calls == [True, False]
        assert store.load().sources == (source,)
        assert client.calls == []


def test_invalid_metadata_and_failed_save_emit_no_success_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        current = store.load()
        store.save(
            replace(
                current, playlists=(replace(current.playlists[0], source_ids=("ser", source.id)),)
            )
        )
        cookie = _login(server)
        _, _, edit = _request(
            server, "GET", f"/admin/sources/edit?source_id={source.id}", cookie=cookie
        )
        invalid = _update_form(_csrf(edit))
        invalid["origin_kind"] = "country"
        invalid["origin_code"] = "UN"
        assert (
            _request(server, "POST", "/admin/sources/update", cookie=cookie, form=invalid)[0] == 400
        )
        _, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)

        def fail_save(state: ManagedState) -> None:
            del state
            raise OSError("secret-path /data/internal")

        monkeypatch.setattr(store, "save", fail_save)
        status, _, body = _request(
            server,
            "POST",
            "/admin/sources/set-enabled",
            cookie=cookie,
            form=_lifecycle_form(_csrf(page), enabled="0"),
        )
        assert status == 500 and b"secret-path" not in body
        assert store.load().sources == (source,)
        assert _Handler.diagnostic_store.list_events() == ()
        assert client.calls == []


def test_source_csrf_cannot_cross_sessions_or_be_reused(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        current = store.load()
        store.save(
            replace(
                current, playlists=(replace(current.playlists[0], source_ids=("ser", source.id)),)
            )
        )
        cookie_a = _login(server)
        cookie_b = _login(server)
        _, _, page = _request(server, "GET", "/admin/sources", cookie=cookie_a)
        token = _csrf(page)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/set-enabled",
                cookie=cookie_b,
                form=_lifecycle_form(token, enabled="0"),
            )[0]
            == 403
        )
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/set-enabled",
                cookie=cookie_a,
                form=_lifecycle_form(token, enabled="0"),
            )[0]
            == 303
        )
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/set-enabled",
                cookie=cookie_a,
                form=_lifecycle_form(token, enabled="1"),
            )[0]
            == 403
        )
        assert store.load().sources == (replace(source, enabled=False),)
        assert client.calls == []


def test_edited_operator_name_is_escaped_in_sources_and_edit(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        cookie = _login(server)
        _, _, edit = _request(
            server, "GET", f"/admin/sources/edit?source_id={source.id}", cookie=cookie
        )
        form = _update_form(_csrf(edit))
        form["display_name"] = "<img src=x onerror=alert(1)>"
        assert _request(server, "POST", "/admin/sources/update", cookie=cookie, form=form)[0] == 303
        _, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        _, _, edit = _request(
            server, "GET", f"/admin/sources/edit?source_id={source.id}", cookie=cookie
        )
        assert b"<img src=x onerror" not in sources + edit
        assert b"&lt;img src=x onerror=alert(1)&gt;" in sources + edit
        assert client.calls == []


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
            current_count = connection.execute(
                "SELECT COUNT(*) FROM spotify_first_episode_state"
            ).fetchone()
            history_count = connection.execute(
                "SELECT COUNT(*) FROM spotify_first_episode_history"
            ).fetchone()
            diagnostic_count = connection.execute(
                "SELECT COUNT(*) FROM diagnostic_events"
            ).fetchone()
        assert current_count == (0,)
        assert history_count == (0,)
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


@pytest.mark.parametrize("kind", ["built_in", "custom", "disabled_custom"])
def test_configured_audit_is_read_only_and_downloadable(tmp_path: Path, kind: str) -> None:
    with _server(tmp_path) as (server, client, store):
        source_id = str(BUILTIN_CATALOG.sources[0].id)
        if kind != "built_in":
            source = _configured_source(store, enabled=kind == "custom")
            source_id = str(source.id)
        before = store.path.read_bytes() if store.path.exists() else None
        with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
            current_before = {
                table: connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in (
                    "spotify_first_episode_state",
                    "spotify_first_source_state",
                    "spotify_public_detector_state",
                    "spotify_first_destination_state",
                )
            }
        with sqlite3.connect(tmp_path / "diagnostics.sqlite3") as connection:
            diagnostics_before = connection.execute(
                "SELECT COUNT(*) FROM diagnostic_events"
            ).fetchone()
        cookie = _login(server)
        status, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200 and b"/admin/sources/audit" in sources
        assert sources.count(b'action="/admin/sources/audit"') == len(BUILTIN_CATALOG.sources) + (
            kind != "built_in"
        )
        assert client.calls == []
        status, review = _audit(server, cookie, source_id)
        assert status == 200
        assert b"Read-only audit" in review and source_id.encode() in review
        assert b"Provider-derived evidence" in review
        assert b"&lt;img src=x onerror=alert(1)&gt;" in review
        assert b"/admin/sources/create" not in review
        assert client.calls == ["details", "episodes"]
        status, _, report = _request(
            server, "GET", "/admin/sources/inspection-report.json", cookie=cookie
        )
        assert status == 200 and client.calls == ["details", "episodes"]
        parsed = json.loads(report)
        assert parsed["schema_version"] == 1
        assert parsed["ownership"] == kind.removeprefix("disabled_")
        assert parsed["source_id"] == source_id
        assert parsed["episodes"] and parsed["public_status"] == "available"
        assert (
            report
            == admin_session_manager_for(_Handler.admin_security)
            .source_inspection(cookie.partition("=")[2])
            .report_json
        )
        for secret in (
            b"secret-provider-body",
            b"secret-refresh-token",
            b"secret-access-token",
            cookie.encode(),
            _csrf(review).encode(),
        ):
            assert secret not in report
        assert store.load().sources == (() if kind == "built_in" else (source,))
        assert (store.path.read_bytes() if store.path.exists() else None) == before
        with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
            for table, rows in current_before.items():
                assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
        with sqlite3.connect(tmp_path / "diagnostics.sqlite3") as connection:
            assert connection.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone() == (
                diagnostics_before
            )
        other_cookie = _login(server)
        assert (
            _request(server, "GET", "/admin/sources/inspection-report.json", cookie=other_cookie)[0]
            == 404
        )
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/create",
                cookie=cookie,
                form=_create_form(review),
            )[0]
            == 400
        )
        assert client.calls == ["details", "episodes"]


def test_configured_audit_auth_csrf_and_stale_identity(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        assert _request(server, "POST", "/admin/sources/audit")[0] == 303
        cookie = _login(server)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/audit",
                cookie=cookie,
                form={"csrf_token": "wrong", "source_id": str(source.id)},
            )[0]
            == 403
        )
        assert client.calls == []
        page = _request(server, "GET", "/admin/sources", cookie=cookie)[2]
        token = _csrf(page)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/audit",
                cookie=cookie,
                form={"csrf_token": token, "source_id": "custom_missing"},
            )[0]
            == 404
        )
        assert client.calls == []
        page = _request(server, "GET", "/admin/sources", cookie=cookie)[2]
        form = {"csrf_token": _csrf(page), "source_id": str(source.id)}
        assert _request(server, "POST", "/admin/sources/audit", cookie=cookie, form=form)[0] == 200
        assert _request(server, "POST", "/admin/sources/audit", cookie=cookie, form=form)[0] == 403
        assert client.calls == ["details", "episodes"]
        store.save(ManagedState())
        assert _audit(server, cookie, str(source.id))[0] == 404
        assert (
            _request(server, "GET", "/admin/sources/inspection-report.json", cookie=cookie)[0]
            == 404
        )
        assert client.calls == ["details", "episodes"]


def test_configured_audit_provider_failure_and_global_cooldown(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        cookie = _login(server)
        client.error = SpotifyApiError(503, "secret-provider-body")
        status, body = _audit(server, cookie, str(source.id))
        assert status == 503 and b"secret-provider-body" not in body
        client.error = SpotifyTransportError("secret-access-token")
        status, body = _audit(server, cookie, str(source.id))
        assert status == 503 and b"secret-access-token" not in body
        client.error = SpotifyApiError(429, "secret-provider-body", retry_after=60)
        status, body = _audit(server, cookie, str(source.id))
        assert status == 503 and b"secret-provider-body" not in body
        assert _Handler.source_admin_service.rate_limit.get() is not None  # type: ignore[union-attr]
        calls = client.calls[:]
        assert _audit(server, cookie, str(source.id))[0] == 503
        assert client.calls == calls
        assert store.load().sources == (source,)


def test_configured_audit_public_failure_is_optional(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store, enabled=False)
        service = _Handler.source_admin_service
        assert service is not None and isinstance(service.public_client, _PublicAudit)
        service.public_client.fail = True
        cookie = _login(server)
        status, review = _audit(server, cookie, str(source.id))
        assert status == 200 and b"unavailable" in review
        report = _request(server, "GET", "/admin/sources/inspection-report.json", cookie=cookie)[2]
        assert json.loads(report)["public_status"] == "unavailable"
        assert b"secret-public-body" not in report + review
        assert client.calls == ["details", "episodes"]


def test_last_effective_source_disable_is_blocked_in_ui_and_backend_even_for_stale_form(
    tmp_path: Path,
) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        sole = store.load()
        store.save(
            replace(sole, playlists=(replace(sole.playlists[0], source_ids=("ser", source.id)),))
        )
        cookie = _login(server)
        _, _, old_page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert b">Disable</button>" in old_page
        store.save(sole)  # Another configuration change removes the alternate enabled source.
        before = store.path.read_bytes()
        _, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        row = next(
            row
            for row in re.findall(rb"<tr>.*?</tr>", page, flags=re.DOTALL)
            if str(source.id).encode() in row
        )
        assert b"Cannot disable:" in row
        assert sole.playlists[0].display_name.encode() in row
        assert b"would have no enabled sources" in row
        assert b"pause the active playlists" in row
        assert b"/admin/sources/set-enabled" not in row
        status, _, body = _request(
            server,
            "POST",
            "/admin/sources/set-enabled",
            cookie=cookie,
            form=_lifecycle_form(_csrf(old_page), enabled="0"),
        )
        assert status == 409
        assert sole.playlists[0].display_name.encode() in body
        assert b"no enabled sources" in body
        assert store.path.read_bytes() == before
        assert _Handler.diagnostic_store.list_events() == ()
        assert client.calls == [] and _Handler.source_admin_service.auth.calls == 0


def test_paused_dormant_assignment_can_be_saved_but_cannot_be_resumed_over_http(
    tmp_path: Path,
) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        state = store.load()
        store.save(replace(state, playlists=(replace(state.playlists[0], enabled=False),)))
        cookie = _login(server)
        _, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert b">Disable</button>" in sources
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/set-enabled",
                cookie=cookie,
                form=_lifecycle_form(_csrf(sources), enabled="0"),
            )[0]
            == 303
        )
        assert store.load().playlists[0].source_ids == (source.id,)
        _, _, page = _request(server, "GET", "/admin/", cookie=cookie)
        assert b"Inactive assignments" in page
        before = store.path.read_bytes()
        status, _, body = _request(
            server,
            "POST",
            "/admin/playlists/update",
            cookie=cookie,
            form=_playlist_form(_csrf(page), [str(source.id)]),
        )
        assert status == 409 and b"at least one enabled source" in body
        assert store.path.read_bytes() == before
        _, _, page = _request(server, "GET", "/admin/", cookie=cookie)
        pause_form = _playlist_form(_csrf(page), [str(source.id)])
        del pause_form["enabled"]
        assert (
            _request(server, "POST", "/admin/playlists/update", cookie=cookie, form=pause_form)[0]
            == 303
        )
        assert store.load().playlists[0].source_ids == (source.id,)
        assert client.calls == [] and _Handler.source_admin_service.auth.calls == 0


def test_pending_activation_blocks_last_source_disable_in_ui_and_http(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, store):
        source = _configured_source(store)
        store.save(ManagedState(sources=(source,)))
        template = BUILTIN_CATALOG.playlist("spain_spanish_news")
        service = _Handler.source_admin_service
        intent = ProvisioningIntent(
            state=ProvisioningState.REQUEST_STARTED,
            template_id=template.id,
            display_name="Pending operator playlist",
            description="",
            cover_id=template.cover_id,
            source_ids=(source.id,),
            retention_hours=template.retention_hours,
            max_episodes=template.max_episodes,
            max_duration_seconds=template.duration_policy.default_max_seconds,
        )
        service.provisioning_journal.save(intent)
        before = store.path.read_bytes()
        cookie = _login(server)
        _, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert b"Cannot disable: Pending activation: Pending operator playlist" in page
        assert b">Disable</button>" not in page
        status, _, body = _request(
            server,
            "POST",
            "/admin/sources/set-enabled",
            cookie=cookie,
            form=_lifecycle_form(_csrf(page), enabled="0"),
        )
        assert status == 409 and b"Pending activation: Pending operator playlist" in body
        assert store.path.read_bytes() == before
        assert service.provisioning_journal.load() == intent
        assert client.calls == [] and service.auth.calls == 0


@pytest.mark.parametrize("repair", ["enable_source", "add_source", "keep_paused"])
def test_real_startup_recovers_supported_legacy_dormant_state_and_allows_admin_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair: str,
) -> None:
    source = ManagedSource(
        id=SourceId("custom_" + SHOW),
        spotify_show_id=SHOW,
        display_name="Legacy news",
        origin=SourceOrigin.country(CountryCode("ES")),
        language=LanguageTag("es"),
        editorial_scope=EditorialScope.NATIONAL,
        timezone="Europe/Madrid",
        parser_id=ParserId("release_date_title"),
        collection_filter_id=None,
        enabled=False,
    )
    playlist = replace(
        activate_template(BUILTIN_CATALOG.playlist("spain_spanish_news"), "preserved-destination"),
        source_ids=(source.id,),
    )
    legacy = ManagedState(sources=(source,), playlists=(playlist,))
    store = ManagedStateStore(tmp_path / "managed-state.json")
    store.save(legacy)  # Supported dormant assignments retain their durable contract.
    assert store.load() == legacy and legacy.schema_version == 6
    old_json = json.loads(store.path.read_text())
    provider_calls: list[str] = []

    def no_provider(*args: Any, **kwargs: Any) -> Any:
        provider_calls.append("unexpected provider I/O")
        raise AssertionError("startup recovery and local Admin repair must not call providers")

    monkeypatch.setattr(engine_runtime, "SpotifyClient", no_provider)
    monkeypatch.setattr(SpotifyAuthService, "get_access_token", no_provider)
    monkeypatch.setattr(engine_runtime.PublicShowClient, "observe_episode", no_provider)
    # Only defer asynchronous cycles after the explicit repair/activation. The real startup,
    # configuration loading, lifecycle decisions and all HTTP handlers remain in use.
    scheduler_started = threading.Event()

    def parked_scheduler(self: Any, stop_event: threading.Event) -> None:
        scheduler_started.set()
        stop_event.wait(timeout=10)

    monkeypatch.setattr(engine_runtime.EngineScheduler, "run", parked_scheduler)
    ready, stop = threading.Event(), threading.Event()
    servers: list[HTTPServer] = []
    outcomes: list[int | Exception] = []

    class Handler(DiagnosticOperationalHealthHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

    def bind_server(address: tuple[str, int], handler: Any) -> HTTPServer:
        server = HTTPServer(address, handler)
        servers.append(server)
        ready.set()
        return server

    monkeypatch.setattr(engine_runtime, "HTTPServer", bind_server)

    def run_application() -> None:
        try:
            outcomes.append(
                engine_runtime.serve(
                    host="127.0.0.1",
                    port=0,
                    data_dir=tmp_path,
                    stop_event=stop,
                    environ={
                        "NEWS_PLAYLIST_ADMIN_MODE": "lan",
                        "NEWS_PLAYLIST_ADMIN_PASSWORD": PASSWORD,
                        "SPOTIFY_CLIENT_ID": "test-client-id",
                    },
                    handler_type=Handler,
                )
            )
        except Exception as exc:
            outcomes.append(exc)
        finally:
            ready.set()

    thread = threading.Thread(target=run_application)
    thread.start()
    try:
        assert ready.wait(timeout=5)
        assert servers, outcomes  # Before the fix, startup fails before binding HTTP/Admin.
        server = servers[0]
        assert _request(server, "GET", "/healthz")[0] == 200
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/", cookie=cookie)
        assert status == 200
        assert b"Paused" in page and b"Inactive assignments" in page
        assert b"This playlist has no enabled sources" in page
        assert b"Enable an assigned source or select another enabled source" in page
        recovered_json = json.loads(store.path.read_text())
        old_json["playlists"][0]["enabled"] = False
        assert recovered_json == old_json  # Only the local active flag changes.
        assert store.load().playlists[0].source_ids == playlist.source_ids
        assert store.load().playlists[0].destination == playlist.destination
        assert not scheduler_started.is_set()
        assert Handler.engine_lifecycle.scheduler is None
        # Even an explicit cycle cannot reach Spotify while the installation is paused.
        cycle = Handler.engine_lifecycle.runner.run_cycle()
        assert cycle.sources == () and cycle.playlists == ()
        assert provider_calls == []
        before = store.path.read_bytes()
        status, _, body = _request(
            server,
            "POST",
            "/admin/playlists/update",
            cookie=cookie,
            form=_playlist_form(_csrf(page), [str(source.id)]),
        )
        assert status == 409 and b"at least one enabled source" in body
        assert store.path.read_bytes() == before
        if repair == "enable_source":
            _, _, sources = _request(server, "GET", "/admin/sources", cookie=cookie)
            assert (
                _request(
                    server,
                    "POST",
                    "/admin/sources/set-enabled",
                    cookie=cookie,
                    form=_lifecycle_form(_csrf(sources), enabled="1"),
                )[0]
                == 303
            )
            assert store.load().playlists[0].source_ids == playlist.source_ids
        _, _, page = _request(server, "GET", "/admin/", cookie=cookie)
        selected = ["ser", str(source.id)] if repair == "add_source" else [str(source.id)]
        form = _playlist_form(_csrf(page), selected)
        if repair == "keep_paused":
            del form["enabled"]
        assert (
            _request(server, "POST", "/admin/playlists/update", cookie=cookie, form=form)[0] == 303
        )
        final = store.load()
        compiled = compile_engine_config(BUILTIN_CATALOG, final)
        assert final.playlists[0].destination == playlist.destination
        if repair == "keep_paused":
            assert final.playlists[0].source_ids == playlist.source_ids
            assert not final.playlists[0].enabled
            assert compiled.playlists[0].source_selection.explicit == ()
            assert engine_runtime._load_runtime_config(tmp_path, {}) is None
            assert not scheduler_started.is_set()
        else:
            expected = (SourceId("ser"),) if repair == "add_source" else (source.id,)
            assert final.playlists[0].enabled
            assert compiled.playlists[0].source_selection.explicit == expected
            runtime_config = engine_runtime._load_runtime_config(tmp_path, {})
            assert runtime_config is not None
            assert runtime_config.playlists[0].source_selection.explicit == expected
            assert scheduler_started.wait(timeout=5)
        assert source.id in final.playlists[0].source_ids
        assert provider_calls == []  # No metadata, cover, catalogue, detector or OAuth I/O.
    finally:
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert outcomes == [0]
