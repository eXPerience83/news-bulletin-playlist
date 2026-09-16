from __future__ import annotations

import io
import logging
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _reconciliation import strict_reconcile_playlist_items

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.diagnostics_web import (
    DiagnosticFilters,
    build_diagnostic_bundle,
    render_diagnostics_page,
)
from news_bulletin_playlist.engine import EngineCycleResult, PlaylistCycleOutcome
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.reconciliation import (
    SpotifyReconciliationError,
)
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify.client import SpotifyApiError
from news_bulletin_playlist.spotify_planner import PlannerReason, PlannerShadowResult

NOW = datetime(2026, 9, 2, 17, 51, 12, tzinfo=UTC)
SECRET = "access-token-sentinel-never-export"


class _Runner:
    def __init__(self, result: EngineCycleResult) -> None:
        self.result = result

    def run_cycle(self) -> EngineCycleResult:
        return self.result


def test_shadow_planner_diagnostic_is_bounded_and_exportable(tmp_path: Path) -> None:
    secret = "access-token-shadow-sentinel"
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    output = io.StringIO()
    logger = logging.Logger("test-shadow-planner-diagnostic")
    logger.addHandler(logging.StreamHandler(output))
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW,
        ok=True,
        sources=(),
        playlists=(),
        planner_shadow=PlannerShadowResult(
            would_need_spotify=True,
            would_allow_local_skip=False,
            reasons=(PlannerReason.DIRTY, PlannerReason.PENDING_VERIFICATION),
            matching_work_count=0,
            matching_source_count=0,
            dirty_destination_count=1,
            catalogue_audit_due_count=0,
            destination_audit_due_count=0,
            safety_blocker_count=1,
        ),
    )
    diagnostics = OperationalDiagnostics(store, logger=logger)
    InstrumentedEngineCycleRunner(_Runner(result), diagnostics, clock=lambda: NOW).run_cycle()
    event = next(
        event
        for event in store.list_events(limit=10)
        if event.event_name == "spotify_planner_shadow_decision"
    )
    assert event.details["planner_shadow_enabled"] is True
    assert event.details["planner_reason_dirty"] is True
    assert event.details["planner_reason_pending_verification"] is True
    assert event.details["planner_would_allow_local_skip"] is False

    page = render_diagnostics_page(events=(event,), filters=DiagnosticFilters(hours=24, limit=200))
    bundle = build_diagnostic_bundle(
        events=(event,),
        generated_at=NOW,
        last_cycle=result,
        retention_days=30,
        max_events=10_000,
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        exported = b"\n".join(archive.read(name) for name in archive.namelist())
    combined = b"\n".join((page, exported, output.getvalue().encode()))
    assert b"spotify_planner_shadow_decision" in combined
    assert secret.encode() not in combined


def test_live_shape_failure_is_safe_in_store_ui_and_zip(tmp_path: Path) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    output = io.StringIO()
    logger = logging.Logger("test-reconciliation-diagnostics-integration")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(output))
    diagnostics = OperationalDiagnostics(store, logger=logger)

    raw_error = (
        "Spotify playlist prewrite response pagination truncated before total "
        f"(offset=50 returned=24 total=80 next=null) {SECRET}"
    )
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=4),
        ok=False,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spain_spanish_news"),
                ok=False,
                desired_count=80,
                applied_count=None,
                wrote=None,
                last_success_at=NOW - timedelta(minutes=10),
                error=raw_error,
            ),
        ),
        error=f"cycle failure includes {SECRET}",
    )

    runner = InstrumentedEngineCycleRunner(
        _Runner(result),
        diagnostics,
        clock=lambda: NOW,
    )
    runner.run_cycle()

    events = store.list_events(limit=10)
    playlist_event = next(
        event for event in events if event.event_name == "playlist_reconciliation_failed"
    )
    assert playlist_event.details == {
        "desired_count": 80,
        "failure_class": "pagination_error",
        "failure_reason": "playlist_pagination_truncated",
        "phase": "prewrite",
        "operation": "playlist_items",
        "offset": 50,
        "returned_count": 24,
        "total": 80,
        "next_state": "null",
        "verification_outcome": "failed",
        "write_decision": "blocked",
    }
    assert SECRET not in repr(events)
    assert SECRET not in output.getvalue()

    page = render_diagnostics_page(
        events=(playlist_event,),
        filters=DiagnosticFilters(hours=24, limit=200),
    )
    assert b"failure_class=pagination_error" in page
    assert b"failure_reason=playlist_pagination_truncated" in page
    assert b"phase=prewrite" in page
    assert b"operation=playlist_items" in page
    assert SECRET.encode() not in page

    bundle = build_diagnostic_bundle(
        events=(playlist_event,),
        generated_at=NOW,
        last_cycle=result,
        retention_days=30,
        max_events=10_000,
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        combined = b"\n".join(archive.read(name) for name in archive.namelist())
    assert b'"failure_class":"pagination_error"' in combined
    assert b'"failure_reason":"playlist_pagination_truncated"' in combined
    assert b'"operation":"playlist_items"' in combined
    assert b'"offset":50' in combined
    assert SECRET.encode() not in combined


def test_ambiguous_502_request_metrics_are_sanitized_in_store_ui_and_zip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinels = (
        "access-token-sentinel",
        "refresh-token-sentinel",
        "authorization-code-sentinel",
        "pkce-verifier-sentinel",
        "spotify:episode:episode-uri-sentinel",
    )

    class _Replace502:
        def playlist_items(self, playlist_id: str, *, limit: int, offset: int) -> dict[str, object]:
            del playlist_id, limit, offset
            return {"items": [], "next": None, "total": 0}

        def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, object]:
            del playlist_id, uris
            raise SpotifyApiError(502, " ".join(sentinels))

        def playlist_snapshot(self, playlist_id: str) -> dict[str, object]:
            del playlist_id
            return {"snapshot_id": "test"}

    try:
        strict_reconcile_playlist_items(
            _Replace502(),  # type: ignore[arg-type]
            "playlist",
            ["spotify:episode:0000000000000000000001"],
        )
    except SpotifyReconciliationError as exc:
        raw_error = str(exc)
    else:  # pragma: no cover - the fake must produce the ambiguous failure
        raise AssertionError("expected the 502 write to fail")

    assert raw_error == (
        "Spotify playlist write replace_items API failure (http_status=502) (write_outcome=unknown)"
    )
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    output = io.StringIO()
    logger = logging.Logger("test-ambiguous-502-sanitization")
    logger.addHandler(logging.StreamHandler(output))
    diagnostics = OperationalDiagnostics(store, logger=logger)
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=2),
        ok=False,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("international_polish_news"),
                ok=False,
                desired_count=41,
                applied_count=None,
                wrote=None,
                last_success_at=None,
                error=raw_error,
                request_item_count=41,
                request_payload_bytes=1732,
                valid_uri_count=41,
                invalid_uri_count=0,
                duplicate_count=0,
                desired_fingerprint="a" * 64,
                desired_changed_since_previous_attempt=False,
                consecutive_failure_count=7,
                first_failure_at=NOW - timedelta(hours=1),
                last_failure_at=NOW,
                write_outcome="unknown",
            ),
        ),
        error="cycle degraded",
    )
    InstrumentedEngineCycleRunner(_Runner(result), diagnostics, clock=lambda: NOW).run_cycle()
    event = next(
        event
        for event in store.list_events(limit=10)
        if event.event_name == "playlist_reconciliation_failed"
    )
    assert event.details["provider"] == "spotify"
    assert event.details["http_status"] == 502
    assert event.details["operation"] == "replace_items"
    assert event.details["phase"] == "write"
    assert event.details["failure_class"] == "api_error"
    assert event.details["failure_reason"] == "replace_items_api_error"
    assert event.details["write_outcome"] == "unknown"
    assert event.details["request_item_count"] == 41
    assert event.details["valid_uri_count"] == 41
    assert event.details["desired_fingerprint"] == "a" * 64
    assert "uri" not in repr(event.details).replace("valid_uri_count", "").replace(
        "invalid_uri_count", ""
    )
    page = render_diagnostics_page(events=(event,), filters=DiagnosticFilters())
    bundle = build_diagnostic_bundle(
        events=(event,),
        generated_at=NOW,
        last_cycle=result,
        retention_days=30,
        max_events=10_000,
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        combined = b"\n".join(archive.read(name) for name in archive.namelist())
    assert b"write_outcome=unknown" in page
    assert b'"write_outcome":"unknown"' in combined
    captured = capsys.readouterr()
    combined_text = "\n".join(
        (
            repr(store.list_events(limit=10)),
            output.getvalue(),
            captured.out,
            captured.err,
            raw_error,
        )
    )
    for sentinel in sentinels:
        assert sentinel not in combined_text
        assert sentinel.encode() not in page + combined
