from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist import diagnostics_runtime, engine_runtime
from news_bulletin_playlist.admin_session import (
    ADMIN_SESSION_COOKIE,
    admin_session_manager_for,
)
from news_bulletin_playlist.diagnostics import (
    DiagnosticEvent,
    DiagnosticEventStore,
    DiagnosticSeverity,
)
from news_bulletin_playlist.diagnostics_runtime import (
    DiagnosticOperationalHealthHandler,
    _diagnostic_display_timezone,
)
from news_bulletin_playlist.diagnostics_web import (
    DiagnosticFilters,
    build_diagnostic_bundle,
    parse_diagnostic_filters,
    render_diagnostics_page,
)
from news_bulletin_playlist.engine import (
    EngineCycleResult,
    OperationalStatus,
    PlaylistCycleOutcome,
    SourceCycleOutcome,
)
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.persistence import PersistenceError
from news_bulletin_playlist.runtime import AdminSecurity

NOW = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
PASSWORD = "long-enough-admin-password"
SECRET = "access-token-sentinel-never-export"


def _event() -> DiagnosticEvent:
    return DiagnosticEvent(
        event_id=7,
        occurred_at=NOW,
        severity=DiagnosticSeverity.WARNING,
        component="playlist",
        event_name="destination_preserved",
        cycle_id="cycle-20260902T100000000000Z",
        source_id=None,
        playlist_id="spain_spanish_news",
        details={"desired_count": 12, "write_decision": "preserved"},
    )


def _failed_cycle() -> EngineCycleResult:
    return EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=2),
        ok=False,
        sources=(
            SourceCycleOutcome(
                source_id=SourceId("rne"),
                ok=False,
                episode_count=0,
                refreshed=False,
                refresh_reason="change",
                api_request_count=0,
                last_detector_success_at=NOW - timedelta(minutes=2),
                last_catalogue_refresh_at=NOW - timedelta(hours=3),
                detector_ok=False,
                detector_changed=True,
                error=f"provider failed with {SECRET}",
            ),
        ),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spain_spanish_news"),
                ok=False,
                desired_count=12,
                applied_count=None,
                wrote=None,
                last_success_at=None,
                error=f"destination failure {SECRET}",
            ),
        ),
        error=f"cycle failure {SECRET}",
    )


def _basic_auth() -> str:
    encoded = base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
    return f"Basic {encoded}"


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


def _serve_one(server: HTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    return thread


def _no_redirect_open(request: urllib.request.Request) -> urllib.error.HTTPError:
    opener = urllib.request.build_opener(_NoRedirect())
    with pytest.raises(urllib.error.HTTPError) as raised:
        opener.open(request, timeout=2)
    return raised.value


def _request(
    url: str,
    *,
    authenticated: bool = False,
    cookie: str | None = None,
) -> urllib.request.Request:
    request = urllib.request.Request(url)
    if cookie is not None:
        request.add_header("Cookie", cookie)
    elif authenticated:
        request.add_header("Authorization", _basic_auth())
    return request


def test_diagnostics_serve_injects_handler_without_mutating_engine_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_handler = engine_runtime.OperationalHealthHandler
    handler = DiagnosticOperationalHealthHandler
    previous_timezone = handler.diagnostic_timezone
    previous_timezone_label = handler.diagnostic_timezone_label
    previous_build_revision = handler.build_revision
    observed: dict[str, object] = {}

    def fake_serve(**kwargs: object) -> int:
        assert engine_runtime.OperationalHealthHandler is original_handler
        observed.update(kwargs)
        assert handler.diagnostic_timezone_label == "Europe/Madrid"
        return 17

    monkeypatch.setattr(engine_runtime, "serve", fake_serve)

    assert (
        diagnostics_runtime.serve(
            host="127.0.0.1",
            port=0,
            data_dir=tmp_path,
            stop_event=threading.Event(),
            environ={"TZ": "Europe/Madrid"},
        )
        == 17
    )
    assert observed["handler_type"] is handler
    assert engine_runtime.OperationalHealthHandler is original_handler
    assert handler.diagnostic_timezone is previous_timezone
    assert handler.diagnostic_timezone_label == previous_timezone_label
    assert handler.build_revision == previous_build_revision


def test_diagnostic_filters_are_bounded_and_single_valued() -> None:
    filters = parse_diagnostic_filters(
        "severity=warning&source=rne&playlist=spain_spanish_news&hours=168&limit=50"
    )
    assert filters == DiagnosticFilters(
        severity=DiagnosticSeverity.WARNING,
        source_id="rne",
        playlist_id="spain_spanish_news",
        hours=168,
        limit=50,
    )
    assert filters.since(now=NOW) == NOW - timedelta(days=7)

    with pytest.raises(ValueError, match="at most once"):
        parse_diagnostic_filters("severity=INFO&severity=ERROR")
    with pytest.raises(ValueError, match="unknown diagnostics filter"):
        parse_diagnostic_filters("debug=1")
    with pytest.raises(ValueError, match="between 1 and 500"):
        parse_diagnostic_filters("limit=501")
    with pytest.raises(ValueError, match="invalid identifier"):
        parse_diagnostic_filters("source=../../spotify-auth.json")


def test_diagnostics_page_contains_only_sanitized_event_fields() -> None:
    payload = render_diagnostics_page(
        events=(_event(),),
        filters=DiagnosticFilters(hours=24, limit=200),
    ).decode()

    assert "destination_preserved" in payload
    assert "write_decision=preserved" in payload
    assert "/admin/diagnostics/export.zip?" in payload
    assert "limit=500" in payload
    assert SECRET not in payload


def test_public_response_capture_is_hidden_inline_and_exported_as_exact_attachment() -> None:
    raw_body = b"<html><body>simulated-public-schema-drift</body></html>"
    event = DiagnosticEvent(
        event_id=42,
        occurred_at=NOW,
        severity=DiagnosticSeverity.WARNING,
        component="spotify.public_detector",
        event_name="public_detector_failed",
        cycle_id="cycle-public-schema-test",
        source_id="ser",
        playlist_id=None,
        details={
            "detector_outcome": "response_failure",
            "detector_response_reason": "embed_shape",
            "public_response_capture": True,
            "public_response_surface": "embed",
            "public_response_content_type": "text/html",
            "public_response_size_bytes": len(raw_body),
            "public_response_sha256": hashlib.sha256(raw_body).hexdigest(),
            "public_response_truncated": False,
            "public_response_body_b64": base64.b64encode(raw_body).decode("ascii"),
        },
    )

    page = render_diagnostics_page(
        events=(event,),
        filters=DiagnosticFilters(hours=24, limit=200),
    )
    assert raw_body not in page
    assert b"public_response_body=&lt;captured in ZIP&gt;" in page
    assert b"detector_response_reason=embed_shape" in page

    bundle = build_diagnostic_bundle(
        events=(event,),
        generated_at=NOW,
        last_cycle=None,
        retention_days=30,
        max_events=10_000,
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        names = set(archive.namelist())
        attachment = "public-responses/event-42-ser-embed.html"
        assert attachment in names
        assert archive.read(attachment) == raw_body
        assert raw_body not in archive.read("diagnostics.jsonl")
        assert raw_body not in archive.read("diagnostics.txt")
        event_doc = json.loads(archive.read("diagnostics.jsonl").decode().strip())
        manifest = json.loads(archive.read("manifest.json"))
    assert "public_response_body_b64" not in event_doc["details"]
    assert event_doc["details"]["public_response_attachment"] == attachment
    assert attachment in manifest["files"]


def test_diagnostics_page_renders_configured_timezone_with_dst() -> None:
    madrid = ZoneInfo("Europe/Madrid")
    summer_payload = render_diagnostics_page(
        events=(_event(),),
        filters=DiagnosticFilters(),
        display_timezone=madrid,
        timezone_label="Europe/Madrid",
    ).decode()
    winter_event = replace(
        _event(),
        occurred_at=datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
    )
    winter_payload = render_diagnostics_page(
        events=(winter_event,),
        filters=DiagnosticFilters(),
        display_timezone=madrid,
        timezone_label="Europe/Madrid",
    ).decode()

    assert "Time (Europe/Madrid)" in summer_payload
    assert "2026-09-02T12:00:00+02:00" in summer_payload
    assert "2026-01-15T11:00:00+01:00" in winter_payload


def test_diagnostic_display_timezone_uses_tz_and_falls_back_to_utc() -> None:
    madrid, label = _diagnostic_display_timezone({"TZ": "Europe/Madrid"})
    assert label == "Europe/Madrid"
    assert NOW.astimezone(madrid).isoformat(timespec="seconds").endswith("+02:00")

    fallback, fallback_label = _diagnostic_display_timezone({"TZ": "Invalid/Timezone"})
    assert fallback_label == "UTC"
    assert NOW.astimezone(fallback) == NOW


def test_diagnostic_bundle_never_copies_raw_cycle_errors() -> None:
    payload = build_diagnostic_bundle(
        events=(_event(),),
        generated_at=NOW,
        last_cycle=_failed_cycle(),
        retention_days=30,
        max_events=10_000,
    )

    assert SECRET.encode() not in payload
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert set(archive.namelist()) == {
            "diagnostics.jsonl",
            "diagnostics.txt",
            "manifest.json",
            "runtime.json",
            "status.json",
        }
        jsonl = archive.read("diagnostics.jsonl")
        runtime = json.loads(archive.read("runtime.json"))
        combined = b"\n".join(archive.read(name) for name in archive.namelist())
    assert SECRET.encode() not in combined
    assert b"destination_preserved" in combined
    assert b'"source_id": "rne"' in combined
    assert b'"occurred_at":"2026-09-02T10:00:00Z"' in jsonl
    assert b"+02:00" not in jsonl
    assert runtime["exported_event_count"] == 1
    assert runtime["export_requested_limit"] == 200
    assert runtime["export_max_allowed_limit"] == 500
    assert runtime["export_result_truncated"] is False
    assert runtime["export_first_event_id"] == 7
    assert runtime["export_last_event_id"] == 7
    assert runtime["export_filters"] == {
        "hours": 24,
        "playlist_id": None,
        "severity": None,
        "source_id": None,
    }
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        status = json.loads(archive.read("status.json"))
    source = status["last_cycle"]["sources"][0]
    assert "last_success_at" not in source
    assert source["last_detector_success_at"] == "2026-09-02T09:58:00Z"
    assert source["last_catalogue_refresh_at"] == "2026-09-02T07:00:00Z"


def test_blocked_source_diagnostics_zip_does_not_invent_zero_desired() -> None:
    cycle = _failed_cycle()
    blocked = replace(
        cycle.playlists[0],
        desired_count=0,
        error="destination preserved: assigned source coverage unproven",
        blocked_source_ids=(SourceId("rne"),),
        block_reason="coverage_unproven_null_slot",
    )
    payload = build_diagnostic_bundle(
        events=(),
        generated_at=NOW,
        last_cycle=replace(cycle, playlists=(blocked,)),
        retention_days=30,
        max_events=10_000,
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        status = json.loads(archive.read("status.json"))
    recorded = status["last_cycle"]["playlists"][0]
    assert recorded["desired_count"] is None
    assert recorded["blocked_source_ids"] == ["rne"]
    assert recorded["block_reason"] == "coverage_unproven_null_slot"
    assert recorded["last_success_at"] is None
    assert status["last_cycle"]["sources"][0]["null_item_count"] == 0


def test_diagnostic_export_truncation_uses_total_matching_count(tmp_path: Path) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    observed = datetime.now(UTC)
    for index in range(3):
        store.record(
            occurred_at=observed + timedelta(seconds=index),
            severity=DiagnosticSeverity.INFO,
            component="engine",
            event_name="cycle_completed",
            cycle_id=f"cycle-{index}",
        )
    handler = type("DiagnosticHandler", (), {"diagnostic_store": store})()

    filters, events, truncated = DiagnosticOperationalHealthHandler._diagnostic_events(
        handler, "hours=all&limit=2"
    )

    assert filters.limit == 2
    assert len(events) == 2
    assert truncated is True

    _, exact_events, exact_truncated = DiagnosticOperationalHealthHandler._diagnostic_events(
        handler, "hours=all&limit=3"
    )

    assert len(exact_events) == 3
    assert exact_truncated is False


def test_diagnostic_export_uses_bounded_event_snapshot(tmp_path: Path) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    event = store.record(
        occurred_at=NOW,
        severity=DiagnosticSeverity.WARNING,
        component="spotify",
        event_name="spotify_rate_limit_backoff",
    )
    handler = type("DiagnosticHandler", (), {"diagnostic_store": store})()

    filters, events, truncated = DiagnosticOperationalHealthHandler._diagnostic_events(
        handler, "hours=all&limit=2"
    )
    page = render_diagnostics_page(events=events, filters=filters)
    bundle = build_diagnostic_bundle(
        events=events,
        generated_at=NOW,
        last_cycle=None,
        retention_days=30,
        max_events=10_000,
        filters=filters,
        result_truncated=truncated,
    )

    assert len(events) == 1
    assert events[0].event_id == event
    assert truncated is False
    assert b"spotify_rate_limit_backoff" in page
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        runtime = json.loads(archive.read("runtime.json"))
        combined = b"\n".join(archive.read(name) for name in archive.namelist())
    assert b"spotify_rate_limit_backoff" in combined
    assert runtime["export_result_truncated"] is False


def test_diagnostic_snapshot_failure_remains_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    handler = type("DiagnosticHandler", (), {"diagnostic_store": store})()

    def fail_snapshot(**_kwargs: object) -> tuple[tuple[DiagnosticEvent, ...], bool]:
        raise PersistenceError("snapshot unavailable")

    monkeypatch.setattr(store, "list_events_with_truncation", fail_snapshot)
    with pytest.raises(RuntimeError, match="diagnostic store unavailable"):
        DiagnosticOperationalHealthHandler._diagnostic_events(handler, "hours=all&limit=2")


def test_diagnostics_routes_require_admin_and_public_status_hides_raw_errors(
    tmp_path: Path,
) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.record(
        occurred_at=NOW,
        severity=DiagnosticSeverity.WARNING,
        component="playlist",
        event_name="destination_preserved",
        cycle_id="cycle-20260902T100000000000Z",
        playlist_id="spain_spanish_news",
        details={"desired_count": 12, "write_decision": "preserved"},
    )
    # Simulate an unchanged diagnostic row persisted by a pre-inline runtime.
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO diagnostic_events "
            "(occurred_at, severity, component, event_name, details_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "2026-09-02T10:00:00Z",
                "INFO",
                "admin",
                "admin_spotify_operation_completed",
                json.dumps(
                    {
                        "metadata_outcome": "applied_without_attribution",
                        "operation": "playlist_metadata",
                        "outcome": "applied",
                    }
                ),
            ),
        )
    status = OperationalStatus(configured=True)
    status.finish_cycle(_failed_cycle(), next_run_at=NOW + timedelta(minutes=10))

    handler = DiagnosticOperationalHealthHandler
    previous = (
        handler.data_dir,
        handler.admin_security,
        handler.spotify_auth,
        handler.operational_status,
        handler.diagnostic_store,
        handler.diagnostic_timezone,
        handler.diagnostic_timezone_label,
    )
    handler.data_dir = tmp_path
    security = AdminSecurity(PASSWORD)
    handler.admin_security = security
    handler.spotify_auth = None
    handler.operational_status = status
    handler.diagnostic_store = store
    handler.diagnostic_timezone = ZoneInfo("Europe/Madrid")
    handler.diagnostic_timezone_label = "Europe/Madrid"
    server = HTTPServer(("127.0.0.1", 0), handler)
    base_url = f"http://127.0.0.1:{server.server_port}"
    session_id = admin_session_manager_for(security).create_session()
    admin_cookie = f"{ADMIN_SESSION_COOKIE}={session_id}"

    try:
        thread = _serve_one(server)
        try:
            anonymous = _no_redirect_open(_request(f"{base_url}/admin/diagnostics"))
        finally:
            thread.join(timeout=2)
        assert anonymous.code == HTTPStatus.SEE_OTHER
        assert anonymous.headers.get("Location") == "/admin/login"
        assert anonymous.headers.get("WWW-Authenticate") is None

        thread = _serve_one(server)
        try:
            with urllib.request.urlopen(
                _request(f"{base_url}/admin/diagnostics?hours=all", cookie=admin_cookie),
                timeout=2,
            ) as response:
                diagnostics_body = response.read()
        finally:
            thread.join(timeout=2)
        assert b"metadata_outcome=applied_without_attribution" in diagnostics_body
        assert b"destination_preserved" in diagnostics_body
        assert b"Time (Europe/Madrid)" in diagnostics_body
        assert b"2026-09-02T12:00:00+02:00" in diagnostics_body
        assert SECRET.encode() not in diagnostics_body

        thread = _serve_one(server)
        try:
            with urllib.request.urlopen(
                _request(
                    f"{base_url}/admin/diagnostics/export.zip?hours=all",
                    cookie=admin_cookie,
                ),
                timeout=2,
            ) as response:
                export_body = response.read()
                disposition = response.headers.get("Content-Disposition")
        finally:
            thread.join(timeout=2)
        assert disposition == 'attachment; filename="news-playlist-diagnostics.zip"'
        with zipfile.ZipFile(io.BytesIO(export_body)) as archive:
            combined_export = b"\n".join(archive.read(name) for name in archive.namelist())
            exported_jsonl = archive.read("diagnostics.jsonl")
        assert SECRET.encode() not in combined_export
        assert b'"occurred_at":"2026-09-02T10:00:00Z"' in exported_jsonl
        assert b"+02:00" not in exported_jsonl
        assert any(
            json.loads(line)["details"].get("metadata_outcome") == "applied_without_attribution"
            for line in exported_jsonl.splitlines()
        )

        thread = _serve_one(server)
        try:
            with urllib.request.urlopen(_request(f"{base_url}/"), timeout=2) as response:
                public_body = response.read()
        finally:
            thread.join(timeout=2)
        assert b"Failed" in public_body
        assert b"see admin diagnostics" in public_body
        assert SECRET.encode() not in public_body
    finally:
        server.server_close()
        (
            handler.data_dir,
            handler.admin_security,
            handler.spotify_auth,
            handler.operational_status,
            handler.diagnostic_store,
            handler.diagnostic_timezone,
            handler.diagnostic_timezone_label,
        ) = previous
