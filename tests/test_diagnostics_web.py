from __future__ import annotations

import base64
import io
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

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
                collection_ok=False,
                matching_ok=None,
                edition_count=0,
                matched_count=0,
                last_success_at=None,
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


def _serve_one(server: HTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    return thread


def _request(url: str, *, authenticated: bool = False) -> urllib.request.Request:
    request = urllib.request.Request(url)
    if authenticated:
        request.add_header("Authorization", _basic_auth())
    return request


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
    assert SECRET not in payload


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
        combined = b"\n".join(archive.read(name) for name in archive.namelist())
    assert SECRET.encode() not in combined
    assert b"destination_preserved" in combined
    assert b'"source_id": "rne"' in combined
    assert b'"occurred_at":"2026-09-02T10:00:00Z"' in jsonl
    assert b"+02:00" not in jsonl


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
    handler.admin_security = AdminSecurity(PASSWORD)
    handler.spotify_auth = None
    handler.operational_status = status
    handler.diagnostic_store = store
    handler.diagnostic_timezone = ZoneInfo("Europe/Madrid")
    handler.diagnostic_timezone_label = "Europe/Madrid"
    server = HTTPServer(("127.0.0.1", 0), handler)
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        thread = _serve_one(server)
        try:
            with pytest.raises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(
                    _request(f"{base_url}/admin/diagnostics"),
                    timeout=2,
                )
        finally:
            thread.join(timeout=2)
        assert raised.value.code == HTTPStatus.UNAUTHORIZED

        thread = _serve_one(server)
        try:
            with urllib.request.urlopen(
                _request(f"{base_url}/admin/diagnostics?hours=all", authenticated=True),
                timeout=2,
            ) as response:
                diagnostics_body = response.read()
        finally:
            thread.join(timeout=2)
        assert b"destination_preserved" in diagnostics_body
        assert b"Time (Europe/Madrid)" in diagnostics_body
        assert b"2026-09-02T12:00:00+02:00" in diagnostics_body
        assert SECRET.encode() not in diagnostics_body

        thread = _serve_one(server)
        try:
            with urllib.request.urlopen(
                _request(
                    f"{base_url}/admin/diagnostics/export.zip?hours=all",
                    authenticated=True,
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
