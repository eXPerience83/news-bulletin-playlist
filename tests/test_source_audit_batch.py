from __future__ import annotations

import http.client
import io
import json
import re
import sqlite3
import threading
import time
import urllib.parse
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist import source_audit_jobs as audit_jobs_module
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.diagnostics_runtime import DiagnosticOperationalHealthHandler
from news_bulletin_playlist.engine import OperationalStatus
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.lan_admin import LanAdminSecurity
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_state import (
    ManagedSource,
    ManagedState,
    ManagedStateStore,
    effective_sources,
)
from news_bulletin_playlist.models import SourceDefinition, SourceId
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.source_admin import (
    MAX_SOURCE_AUDIT_BATCH_SOURCES,
    SourceAdminService,
    SourceAuditBatch,
    SourceAuditBatchItem,
)
from news_bulletin_playlist.source_audit_export import build_source_audit_bundle
from news_bulletin_playlist.source_audit_jobs import SourceAuditJobs
from news_bulletin_playlist.spotify.client import SpotifyApiError
from news_bulletin_playlist.spotify.public_detector import (
    PublicEpisodeObservation,
    PublicShowDetectorError,
    PublicShowHttpError,
    PublicShowTransportError,
)
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal

PASSWORD = "long-enough-admin-password"
EPISODE_ID = "Z" * 22
CUSTOM_SHOW = "A" * 22


class _CountingStateStore(ManagedStateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.loads = 0

    def load(self) -> ManagedState:
        self.loads += 1
        return super().load()


class _Auth:
    def __init__(self) -> None:
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        del now
        self.calls += 1
        return "secret-access-token"


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.errors: dict[str, Exception] = {}

    def show_details(self, show_id: str) -> dict[str, Any]:
        self.calls.append(("details", show_id))
        error = self.errors.get(show_id)
        if error is not None:
            raise error
        return {
            "id": show_id,
            "type": "show",
            "uri": f"spotify:show:{show_id}",
            "name": f"Show {show_id}",
            "publisher": "Publisher",
            "languages": ["es"],
            "total_episodes": 1,
            "secret": "secret-provider-body",
        }

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.calls.append(("episodes", show_id))
        return {
            "items": [
                {
                    "id": EPISODE_ID,
                    "uri": f"spotify:episode:{EPISODE_ID}",
                    "name": "News bulletin",
                    "release_date": datetime.now(UTC).date().isoformat(),
                    "release_date_precision": "day",
                    "duration_ms": 300_000,
                    "secret": "secret-refresh-token",
                }
            ],
            "total": 1,
            "next": None,
            "limit": limit,
            "offset": offset,
        }


class _PublicAudit:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error_once: Exception | None = None

    def observe_episode(
        self, source_id: SourceId, spotify_show_id: str, *, observed_at: datetime
    ) -> PublicEpisodeObservation:
        self.calls.append(spotify_show_id)
        error = self.error_once
        self.error_once = None
        if error is not None:
            raise error
        return PublicEpisodeObservation(
            source_id=source_id,
            spotify_show_id=spotify_show_id,
            episode_id=EPISODE_ID,
            episode_uri=f"spotify:episode:{EPISODE_ID}",
            name="Public bulletin",
            title="Public bulletin",
            release_date_raw=observed_at.date().isoformat(),
            duration_ms=300_000,
            related_entity_uri=f"spotify:show:{spotify_show_id}",
            observed_at=observed_at,
        )


class _Handler(DiagnosticOperationalHealthHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _Lifecycle:
    def reconcile(self, *, configured: bool) -> None:
        del configured


@contextmanager
def _server(
    tmp_path: Path,
) -> Iterator[tuple[HTTPServer, _Client, _PublicAudit, _CountingStateStore]]:
    store = _CountingStateStore(tmp_path / "managed-state.json")
    synchronization = ConfigurationSynchronization()
    auth = _Auth()
    client = _Client()
    public = _PublicAudit()
    runtime_store = SQLiteStore(tmp_path / "state.sqlite3")
    runtime_store.initialize()

    _Handler.admin_security = LanAdminSecurity(PASSWORD)
    _Handler.data_dir = tmp_path
    _Handler.spotify_auth = None
    _Handler.operational_status = OperationalStatus(configured=False)
    _Handler.engine_lifecycle = _Lifecycle()  # type: ignore[assignment]
    _Handler.auth_synchronization = None
    _Handler.configuration_synchronization = synchronization
    _Handler.managed_admin_service = ManagedAdminService(store)
    _Handler.managed_admin_auth = None
    _Handler.diagnostic_store = None
    _Handler.operational_diagnostics = None
    _Handler.spotify_request_metrics = None
    _Handler.build_revision = "test-build"
    _Handler.source_audit_jobs = SourceAuditJobs()
    _Handler.source_admin_service = SourceAdminService(
        store,
        synchronization,
        auth,
        SpotifyRateLimitJournal(runtime_store),
        None,
        client_factory=lambda token: client,
        public_client=public,
    )

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, client, public, store
    finally:
        _Handler.source_audit_jobs.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    server: HTTPServer,
    method: str,
    path: str,
    *,
    cookie: str | None = None,
    form: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    headers = {} if cookie is None else {"Cookie": cookie}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def _login(server: HTTPServer) -> str:
    status, headers, _ = _request(
        server,
        "POST",
        "/admin/login",
        form={"username": "admin", "password": PASSWORD},
    )
    assert status == HTTPStatus.SEE_OTHER
    return headers["Set-Cookie"].split(";", 1)[0]


def _csrf(body: bytes) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', body)
    assert match is not None
    return match.group(1).decode()


def _show_id(source: SourceDefinition) -> str:
    return next(
        reference.external_id
        for reference in source.external_references
        if reference.system == "spotify" and reference.resource_type == "show"
    )


def _custom_source(service: SourceAdminService, show_id: str = CUSTOM_SHOW) -> ManagedSource:
    return service.validate_new_source(
        show_id=show_id,
        display_name="Custom news",
        origin_kind="country",
        origin_code="ES",
        language="es",
        editorial_scope="NAT",
        timezone="Europe/Madrid",
        parser_id="release_date_title",
        collection_filter_id=None,
    )


def _await_export(
    server: HTTPServer, cookie: str, status_url: str
) -> tuple[int, dict[str, str], bytes]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status, _, page = _request(server, "GET", status_url, cookie=cookie)
        assert status == HTTPStatus.OK
        if b"Status: <strong>succeeded</strong>" in page:
            return _request(
                server, "GET", status_url.replace("/status?", "/download?"), cookie=cookie
            )
        if b"Status: <strong>failed</strong>" in page:
            return HTTPStatus.CONFLICT, {}, page
        if b"Status: <strong>cancelled</strong>" in page:
            return HTTPStatus.CONFLICT, {}, page
        time.sleep(0.015)
    raise AssertionError("background audit did not finish in test deadline")


def _export(
    server: HTTPServer, cookie: str, token: str | None = None
) -> tuple[int, dict[str, str], bytes, str]:
    if token is None:
        status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == HTTPStatus.OK
        token = _csrf(page)
    status, headers, payload = _request(
        server,
        "POST",
        "/admin/sources/audits/export",
        cookie=cookie,
        form={"csrf_token": token},
    )
    if status == HTTPStatus.SEE_OTHER:
        status, headers, payload = _await_export(server, cookie, headers["Location"])
    return status, headers, payload, token


def _archive(payload: bytes) -> tuple[dict[str, Any], zipfile.ZipFile]:
    archive = zipfile.ZipFile(io.BytesIO(payload))
    manifest = json.loads(archive.read("manifest.json"))
    return manifest, archive


def _decompressed_archive(payload: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return b"\n".join(archive.read(name) for name in archive.namelist())


def test_batch_export_uses_one_snapshot_and_includes_disabled_custom(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, public, store):
        service = _Handler.source_admin_service
        assert service is not None
        custom = replace(_custom_source(service), enabled=False)
        store.save(ManagedState(sources=(custom,)))
        before = store.load()
        sources = effective_sources(BUILTIN_CATALOG, before)
        expected_calls = [
            (operation, _show_id(source))
            for source in sources
            for operation in ("details", "episodes")
        ]

        assert _request(server, "POST", "/admin/sources/audits/export")[0] == HTTPStatus.SEE_OTHER
        cookie = _login(server)
        page = _request(server, "GET", "/admin/sources", cookie=cookie)[2]
        assert b"Export all audits" in page
        assert page.count(b'action="/admin/sources/audit"') == len(sources)
        token = _csrf(page)

        store.loads = 0
        status, headers, payload, _ = _export(server, cookie, token)
        assert store.loads == 1
        assert status == HTTPStatus.OK
        assert headers["Content-Type"] == "application/zip"
        assert headers["Cache-Control"] == "no-store"
        assert headers["Content-Disposition"].startswith('attachment; filename="source-audits-')
        assert client.calls == expected_calls
        assert len(public.calls) == len(sources)

        manifest, archive = _archive(payload)
        assert manifest["schema_version"] == 1
        assert manifest["build_revision"] == "test-build"
        assert manifest["counts"] == {
            "total": len(sources),
            "attempted": len(sources),
            "succeeded": len(sources),
            "failed": 0,
            "not_attempted": 0,
        }
        entries = {entry["source_id"]: entry for entry in manifest["sources"]}
        assert entries[str(custom.id)]["enabled"] is False
        assert entries[str(custom.id)]["ownership"] == "custom"
        assert entries[str(custom.id)]["status"] == "succeeded"
        assert archive.read(f"sources/{custom.id}.json")
        for source in sources:
            report = json.loads(archive.read(f"sources/{source.id}.json"))
            assert report["source_id"] == str(source.id)
            assert datetime.fromisoformat(report["inspected_at"]) == datetime.fromisoformat(
                manifest["generated_at"]
            )
            assert datetime.fromisoformat(report["cutoff_at"]) == datetime.fromisoformat(
                manifest["generated_at"]
            ) - timedelta(hours=48)
        archive.close()
        assert b"secret-access-token" not in _decompressed_archive(payload)
        assert b"secret-refresh-token" not in _decompressed_archive(payload)
        assert b"secret-provider-body" not in _decompressed_archive(payload)
        assert store.load() == before

        reused_status = _request(
            server,
            "POST",
            "/admin/sources/audits/export",
            cookie=cookie,
            form={"csrf_token": token},
        )[0]
        assert reused_status == HTTPStatus.FORBIDDEN


def test_batch_export_continues_after_source_local_failure(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, _public, _store):
        first = BUILTIN_CATALOG.sources[0]
        first_show = _show_id(first)
        client.errors[first_show] = SpotifyApiError(403, "secret-provider-body")
        cookie = _login(server)

        status, _, payload, _ = _export(server, cookie)
        assert status == HTTPStatus.OK
        manifest, archive = _archive(payload)
        first_entry = manifest["sources"][0]
        assert first_entry["source_id"] == str(first.id)
        assert first_entry["status"] == "failed"
        assert first_entry["failure"] == "forbidden"
        assert first_entry["report_path"] is None
        assert manifest["counts"]["failed"] == 1
        assert manifest["counts"]["succeeded"] == len(BUILTIN_CATALOG.sources) - 1
        assert len(client.calls) == 1 + 2 * (len(BUILTIN_CATALOG.sources) - 1)
        assert b"secret-provider-body" not in _decompressed_archive(payload)
        assert f"sources/{first.id}.json" not in archive.namelist()
        archive.close()


@pytest.mark.parametrize(
    ("status_code", "expected_failure"),
    [
        (401, "authorization"),
        (429, "rate_limited"),
        (503, "unavailable"),
        (504, "unavailable"),
    ],
)
def test_batch_export_global_provider_failure_stops_remaining_work(
    tmp_path: Path,
    status_code: int,
    expected_failure: str,
) -> None:
    with _server(tmp_path) as (server, client, _public, _store):
        first = BUILTIN_CATALOG.sources[0]
        client.errors[_show_id(first)] = SpotifyApiError(
            status_code,
            "secret-provider-body",
            retry_after=60 if status_code == 429 else None,
        )
        cookie = _login(server)

        status, _, payload, _ = _export(server, cookie)
        assert status == HTTPStatus.OK
        manifest, archive = _archive(payload)
        assert manifest["sources"][0]["status"] == "failed"
        assert manifest["sources"][0]["failure"] == expected_failure
        assert all(
            entry["status"] == "not_attempted"
            and entry["not_attempted_reason"] == f"batch_stopped_{expected_failure}"
            for entry in manifest["sources"][1:]
        )
        assert manifest["counts"] == {
            "total": len(BUILTIN_CATALOG.sources),
            "attempted": 1,
            "succeeded": 0,
            "failed": 1,
            "not_attempted": len(BUILTIN_CATALOG.sources) - 1,
        }
        assert client.calls == [("details", _show_id(first))]
        assert b"secret-provider-body" not in _decompressed_archive(payload)
        assert archive.namelist() == ["manifest.json"]
        archive.close()


@pytest.mark.parametrize(
    "public_error",
    [
        PublicShowTransportError("secret-public-body"),
        PublicShowHttpError(429),
        PublicShowHttpError(503),
    ],
)
def test_batch_export_suppresses_public_after_global_public_failure(
    tmp_path: Path,
    public_error: Exception,
) -> None:
    with _server(tmp_path) as (server, _client, public, _store):
        public.error_once = public_error
        cookie = _login(server)

        status, _, payload, _ = _export(server, cookie)
        assert status == HTTPStatus.OK
        manifest, archive = _archive(payload)
        first = BUILTIN_CATALOG.sources[0]
        assert public.calls == [_show_id(first)]
        assert manifest["public_observation_suppressed_after_source"] == str(first.id)
        assert manifest["sources"][0]["public_status"] == "unavailable"
        assert all(entry["public_status"] == "not_requested" for entry in manifest["sources"][1:])
        assert b"secret-public-body" not in _decompressed_archive(payload)
        archive.close()


def test_batch_export_keeps_public_observations_after_source_local_public_failure(
    tmp_path: Path,
) -> None:
    with _server(tmp_path) as (server, _client, public, _store):
        public.error_once = PublicShowDetectorError("secret-public-body")
        cookie = _login(server)

        status, _, payload, _ = _export(server, cookie)
        assert status == HTTPStatus.OK
        manifest, archive = _archive(payload)
        assert len(public.calls) == len(BUILTIN_CATALOG.sources)
        assert manifest["public_observation_suppressed_after_source"] is None
        assert manifest["sources"][0]["public_status"] == "unavailable"
        assert all(entry["public_status"] == "available" for entry in manifest["sources"][1:])
        assert b"secret-public-body" not in _decompressed_archive(payload)
        archive.close()


def test_batch_export_rejects_more_than_hard_cap_before_provider_work(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, client, public, store):
        service = _Handler.source_admin_service
        assert service is not None
        custom_needed = MAX_SOURCE_AUDIT_BATCH_SOURCES - len(BUILTIN_CATALOG.sources) + 1
        custom = tuple(
            _custom_source(service, f"{index:022d}") for index in range(1, custom_needed + 1)
        )
        store.save(ManagedState(sources=custom))
        cookie = _login(server)

        status, _, body, _ = _export(server, cookie)
        assert status == HTTPStatus.CONFLICT
        assert str(MAX_SOURCE_AUDIT_BATCH_SOURCES).encode() in body
        assert client.calls == []
        assert public.calls == []


def test_source_audit_bundle_rejects_unsafe_archive_source_id() -> None:
    batch = SourceAuditBatch(
        generated_at=datetime.now(UTC),
        items=(
            SourceAuditBatchItem(
                source_id=SourceId("../escape"),
                display_name="Unsafe",
                ownership="custom",
                enabled=False,
                status="not_attempted",
                not_attempted_reason="test",
            ),
        ),
    )
    with pytest.raises(ValueError, match="unsafe source id"):
        build_source_audit_bundle(batch, build_revision="test-build")


@pytest.mark.parametrize("item_kind", [None, "string", "array"])
def test_batch_catalogue_failure_keeps_17_source_export_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    item_kind: str | None,
) -> None:
    with _server(tmp_path) as (server, client, public, store):
        service = _Handler.source_admin_service
        assert service is not None
        custom = tuple(_custom_source(service, f"{index:022d}") for index in range(4))
        store.save(ManagedState(sources=custom))
        before = store.path.read_bytes()
        sources = effective_sources(BUILTIN_CATALOG, store.load())
        assert len(sources) == 17
        failed_source = sources[0]
        failed_show = _show_id(failed_source)
        original = client.show_episodes
        secret_episode_id = "S" * 22

        def malformed(show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
            page = original(show_id, limit=limit, offset=offset)
            if show_id == failed_show:
                page["items"][0].update(
                    id=secret_episode_id,
                    uri=f"spotify:episode:{secret_episode_id}",
                    name="secret-title",
                    duration_ms="secret-rejected-value",
                    Authorization="Bearer secret-access-token",
                    raw_json="secret-provider-body",
                    external_url="https://secret-external-url.test/",
                )
                if item_kind is not None:
                    page["items"][0] = {
                        "null": None,
                        "string": "secret-provider-string",
                        "array": ["secret-provider-array-entry", page["items"][0]],
                    }[item_kind]
            return page

        monkeypatch.setattr(client, "show_episodes", malformed)

        # Provider stubs expose reads only. Inspect all existing durable tables before/after.
        def durable_rows() -> dict[str, list[tuple[Any, ...]]]:
            with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
                return {
                    table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
                    for (table,) in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                    if table not in {"diagnostic_events", "sqlite_sequence"}
                }

        service.rate_limit.get()
        rows_before = durable_rows()
        cookie = _login(server)
        status, _, payload, _ = _export(server, cookie)
        assert status == HTTPStatus.OK
        manifest, archive = _archive(payload)
        assert manifest["counts"] == {
            "total": 17,
            "attempted": 17,
            "succeeded": 16,
            "failed": 1,
            "not_attempted": 0,
        }
        failed = manifest["sources"][0]
        assert failed["failure"] == "malformed_response"
        assert failed["response_reason"] == "catalogue_shape"
        assert failed["report_path"] is None
        assert "item_value_kind" not in failed
        assert f"sources/{failed_source.id}.json" not in archive.namelist()
        archive.close()
        serialized = _decompressed_archive(payload)
        for secret in (
            secret_episode_id,
            "secret-title",
            "secret-rejected-value",
            "secret-provider-string",
            "secret-provider-array-entry",
            "Authorization",
            "secret-access-token",
            "secret-refresh-token",
            "secret-provider-body",
            "https://secret-external-url.test/",
        ):
            assert secret.encode() not in serialized
        assert client.calls == [
            (operation, _show_id(source))
            for source in sources
            for operation in ("details", "episodes")
        ]
        assert len(public.calls) == 16
        assert store.path.read_bytes() == before
        assert durable_rows() == rows_before
        diagnostics = service.rate_limit.diagnostics
        assert diagnostics is not None and diagnostics.store is not None
        events = diagnostics.store.list_events(source_id=str(failed_source.id))
        assert len(events) == 1
        expected = {
            "failure_class": "response_shape_error",
            "phase": "collection",
            "catalogue_reason": "invalid_duration" if item_kind is None else "item_not_object",
            "requested_offset": 0,
            "item_index": 0,
        }
        if item_kind is not None:
            expected["item_value_kind"] = item_kind
        assert events[0].details == expected


def test_batch_null_slots_succeeds_for_17_sources_with_degraded_report_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _server(tmp_path) as (server, client, public, store):
        service = _Handler.source_admin_service
        assert service is not None
        custom = tuple(_custom_source(service, f"{index:022d}") for index in range(4))
        store.save(ManagedState(sources=custom))
        before = store.path.read_bytes()
        sources = effective_sources(BUILTIN_CATALOG, store.load())
        affected = sources[0]
        original = client.show_episodes

        def null_page(show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
            page = original(show_id, limit=limit, offset=offset)
            if show_id == _show_id(affected):
                page["items"].insert(0, None)
                page["total"] = 2
                page["raw_body"] = "secret-provider-body"
                page["Authorization"] = "secret-access-token"
                page["external_url"] = "https://secret-external-url.test/"
            return page

        monkeypatch.setattr(client, "show_episodes", null_page)
        # Provider fakes expose reads only. A null does not create catalogue/history tables.
        service.rate_limit.get()
        with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
            tables = [
                name
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            durable_before = {
                name: connection.execute(f'SELECT * FROM "{name}"').fetchall()
                for name in tables
                if name not in {"diagnostic_events", "sqlite_sequence"}
            }
        report = service.audit(str(affected.id))
        assert (
            report.null_item_count == 1
            and not report.catalogue_complete
            and not report.coverage_proven
        )
        assert "catalogue_null_slots_observed" in report.warnings
        assert report.episodes[0].catalogue_position == 2
        client.calls.clear()
        public.calls.clear()
        cookie = _login(server)
        status, _, payload, _ = _export(server, cookie)
        assert status == HTTPStatus.OK
        manifest, archive = _archive(payload)
        assert manifest["counts"] == {
            "total": 17,
            "attempted": 17,
            "succeeded": 17,
            "failed": 0,
            "not_attempted": 0,
        }
        entry = next(item for item in manifest["sources"] if item["source_id"] == str(affected.id))
        assert entry["status"] == "succeeded" and entry["failure"] is None
        document = json.loads(archive.read(entry["report_path"]))
        assert document["null_item_count"] == 1
        assert document["catalogue_complete"] is False and document["coverage_proven"] is False
        assert "catalogue_null_slots_observed" in document["warnings"]
        archive.close()
        assert len(client.calls) == 34 and len(public.calls) == 17
        assert store.path.read_bytes() == before
        serialized = _decompressed_archive(payload)
        for secret in (
            "secret-access-token",
            "secret-refresh-token",
            "secret-provider-body",
            "Authorization",
            "https://secret-external-url.test/",
        ):
            assert secret.encode() not in serialized
        with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
            for table, values in durable_before.items():
                assert connection.execute(f'SELECT * FROM "{table}"').fetchall() == values
            for table in ("spotify_first_episode_state", "spotify_first_source_state"):
                assert (
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name=?", (table,)
                    ).fetchone()
                    is None
                )


def test_background_batch_does_not_block_admin_get_and_rejects_second_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _server(tmp_path) as (server, client, _public, store):
        service = _Handler.source_admin_service
        assert service is not None
        custom_needed = MAX_SOURCE_AUDIT_BATCH_SOURCES - len(BUILTIN_CATALOG.sources)
        store.save(
            ManagedState(
                sources=tuple(
                    _custom_source(service, f"{index:022d}")
                    for index in range(1, custom_needed + 1)
                )
            )
        )
        gate = threading.Event()
        entered = threading.Event()
        original = client.show_details

        def slow_show(show_id: str) -> dict[str, Any]:
            entered.set()
            assert gate.wait(timeout=5), "test never released provider"
            return original(show_id)

        monkeypatch.setattr(client, "show_details", slow_show)
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == HTTPStatus.OK
        token = _csrf(page)
        status, headers, _ = _request(
            server,
            "POST",
            "/admin/sources/audits/export",
            cookie=cookie,
            form={"csrf_token": token},
        )
        assert status == HTTPStatus.SEE_OTHER
        job_url = headers["Location"]
        try:
            assert entered.wait(timeout=5)
            # Unicode can pass str.isalnum(), but compare_digest accepts only ASCII.
            invalid_job = urllib.parse.quote("é" * 12)
            for endpoint in ("status", "download"):
                assert (
                    _request(
                        server,
                        "GET",
                        f"/admin/sources/audits/{endpoint}?job={invalid_job}",
                        cookie=cookie,
                    )[0]
                    == HTTPStatus.NOT_FOUND
                )
            start = time.monotonic()
            status, _, _ = _request(server, "GET", "/admin/sources", cookie=cookie)
            assert status == HTTPStatus.OK
            assert time.monotonic() - start < 1.0
            status, _, progress = _request(server, "GET", job_url, cookie=cookie)
            assert status == HTTPStatus.OK
            assert b"Status: <strong>running</strong>" in progress
            assert b"Refresh" not in progress
            assert _request(server, "GET", job_url)[0] == HTTPStatus.SEE_OTHER
            assert (
                _request(
                    server,
                    "GET",
                    job_url.replace("/status?", "/download?"),
                    cookie=cookie,
                )[0]
                == HTTPStatus.CONFLICT
            )
            status, _, source_page = _request(server, "GET", "/admin/sources", cookie=cookie)
            assert status == HTTPStatus.OK
            assert (
                _request(
                    server,
                    "POST",
                    "/admin/sources/audits/export",
                    cookie=cookie,
                    form={"csrf_token": _csrf(source_page)},
                )[0]
                == HTTPStatus.CONFLICT
            )
            # Each later Admin GET rotates the one-use session CSRF token.
            csrf = _csrf(_request(server, "GET", job_url, cookie=cookie)[2])
            assert (
                _request(
                    server,
                    "POST",
                    "/admin/sources/audits/cancel",
                    cookie=cookie,
                    form={"csrf_token": csrf, "job": "é" * 12},
                )[0]
                == HTTPStatus.NOT_FOUND
            )
            csrf = _csrf(_request(server, "GET", job_url, cookie=cookie)[2])
            job_id = urllib.parse.parse_qs(urllib.parse.urlsplit(job_url).query)["job"][0]
            cancel_status, cancel_headers, _ = _request(
                server,
                "POST",
                "/admin/sources/audits/cancel",
                cookie=cookie,
                form={"csrf_token": csrf, "job": job_id},
            )
            assert cancel_status == HTTPStatus.SEE_OTHER
            assert cancel_headers["Location"] == job_url
        finally:
            gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, _, page = _request(server, "GET", job_url, cookie=cookie)
            if b"Status: <strong>cancelled</strong>" in page:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("cancelled audit did not terminate")
        assert len(client.calls) <= 2
        status, _, _ = _request(
            server,
            "GET",
            job_url.replace("/status?", "/download?"),
            cookie=cookie,
        )
        assert status == HTTPStatus.CONFLICT

        # Single-flight is released once cancellation has reached a terminal state.
        retry_status, _, retry_payload, _ = _export(server, cookie)
        assert retry_status == HTTPStatus.OK
        retry_manifest, retry_archive = _archive(retry_payload)
        assert retry_manifest["counts"]["total"] == MAX_SOURCE_AUDIT_BATCH_SOURCES
        retry_archive.close()


def test_audit_export_persists_sanitized_job_progress_events(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, _client, _public, _store):
        diagnostics = DiagnosticEventStore(tmp_path / "audit-events.sqlite3")
        diagnostics.initialize()
        _Handler.diagnostic_store = diagnostics
        cookie = _login(server)

        status, _, payload, _ = _export(server, cookie)
        assert status == HTTPStatus.OK
        assert payload.startswith(b"PK")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            events = diagnostics.list_events()
            if any(event.event_name == "admin_source_audit_succeeded" for event in events):
                break
            time.sleep(0.015)
        else:
            raise AssertionError("audit completion event was not persisted")

        names = {event.event_name for event in events}
        assert {
            "admin_source_audit_started",
            "admin_source_audit_source_completed",
            "admin_source_audit_succeeded",
        } <= names
        final = next(
            event for event in events if event.event_name == "admin_source_audit_succeeded"
        )
        assert final.details == {
            "operation": "audit_export",
            "completed_sources": len(BUILTIN_CATALOG.sources),
            "total_sources": len(BUILTIN_CATALOG.sources),
        }
        assert "secret-" not in repr(events)


def test_audit_export_deadline_applies_after_zip_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _server(tmp_path) as (server, _client, _public, _store):
        jobs = _Handler.source_audit_jobs
        assert jobs is not None
        original = audit_jobs_module.build_source_audit_bundle

        def expire_during_bundle(batch: SourceAuditBatch, *, build_revision: str) -> bytes:
            payload = original(batch, build_revision=build_revision)
            with jobs._lock:
                jobs._started = time.monotonic() - audit_jobs_module.JOB_MAX_SECONDS - 1
            return payload

        monkeypatch.setattr(
            audit_jobs_module, "build_source_audit_bundle", expire_during_bundle
        )
        status, _, page, _ = _export(server, _login(server))
        assert status == HTTPStatus.CONFLICT
        assert b"Audit exceeded its time limit" in page


def test_audit_result_expiry_releases_zip_and_hides_job(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, _client, _public, _store):
        cookie = _login(server)
        page = _request(server, "GET", "/admin/sources", cookie=cookie)[2]
        status, headers, _ = _request(
            server,
            "POST",
            "/admin/sources/audits/export",
            cookie=cookie,
            form={"csrf_token": _csrf(page)},
        )
        assert status == HTTPStatus.SEE_OTHER
        url = headers["Location"]
        assert _await_export(server, cookie, url)[0] == HTTPStatus.OK
        jobs = _Handler.source_audit_jobs
        assert jobs is not None
        with jobs._lock:
            jobs._finished -= audit_jobs_module.JOB_RESULT_SECONDS + 1
        assert _request(server, "GET", url, cookie=cookie)[0] == HTTPStatus.NOT_FOUND
        assert _request(
            server, "GET", url.replace("/status?", "/download?"), cookie=cookie
        )[0] == HTTPStatus.NOT_FOUND
        assert jobs._job is None


def test_audit_job_status_invalid_url_and_csrf_enforced(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, _client, _public, _store):
        cookie = _login(server)
        assert (
            _request(
                server,
                "GET",
                "/admin/sources/audits/status?job=../../bad",
                cookie=cookie,
            )[0]
            == HTTPStatus.NOT_FOUND
        )
        assert (
            _request(
                server,
                "GET",
                "/admin/sources/audits/status?job=x",
                cookie=cookie,
            )[0]
            == HTTPStatus.NOT_FOUND
        )
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/audits/cancel",
                cookie=cookie,
                form={"csrf_token": "invalid", "job": "any"},
            )[0]
            == HTTPStatus.FORBIDDEN
        )
