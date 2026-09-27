from __future__ import annotations

import http.client
import io
import json
import re
import threading
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


def _export(server: HTTPServer, cookie: str) -> tuple[int, dict[str, str], bytes, str]:
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
        status, headers, payload = _request(
            server,
            "POST",
            "/admin/sources/audits/export",
            cookie=cookie,
            form={"csrf_token": token},
        )
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
    [(401, "authorization"), (429, "rate_limited"), (503, "unavailable")],
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
