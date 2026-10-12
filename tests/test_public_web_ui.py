from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_bulletin_playlist.diagnostics_runtime import _safe_operational_status_page
from news_bulletin_playlist.engine import (
    EngineCycleResult,
    OperationalStatus,
    PlaylistCycleOutcome,
    SourceCycleOutcome,
)
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.spotify.auth import AuthorizationState
from news_bulletin_playlist.web_ui import status_badge

NOW = datetime(2026, 9, 22, 20, 0, tzinfo=UTC)
SECRET = "public-page-secret-sentinel"


def _failed_status() -> OperationalStatus:
    cycle = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=2),
        ok=False,
        sources=(
            SourceCycleOutcome(
                source_id=SourceId("rne"),
                ok=False,
                episode_count=12,
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
                desired_count=8,
                applied_count=None,
                wrote=None,
                last_success_at=NOW - timedelta(minutes=12),
                error=f"destination failed with {SECRET}",
            ),
        ),
        error=f"cycle failed with {SECRET}",
    )
    status = OperationalStatus(configured=True)
    status.finish_cycle(cycle, next_run_at=NOW + timedelta(minutes=10))
    return status


def test_public_status_uses_modern_read_only_shell() -> None:
    payload = _safe_operational_status_page(
        ready=True,
        spotify_state=AuthorizationState.CONNECTED,
        status=OperationalStatus(configured=True),
        build_revision="test-build",
    ).decode()

    assert '<main id="main-content">' in payload
    assert 'class="hero"' in payload
    assert 'class="summary-grid"' in payload
    assert 'aria-labelledby="sources-heading"' in payload
    assert 'aria-labelledby="playlists-heading"' in payload
    assert 'href="/admin/">Administration</a>' in payload
    assert "prefers-color-scheme: light" in payload
    assert "Read-only status" in payload
    assert "Version" in payload
    assert "test-build" in payload
    assert "creating arbitrary new playlist templates is not available yet" in payload
    assert "Copyright (c) 2026 Roberto Ribes" in payload
    assert "<form" not in payload


def test_public_status_keeps_failure_details_sanitized() -> None:
    payload = _safe_operational_status_page(
        ready=True,
        spotify_state=AuthorizationState.CONNECTED,
        status=_failed_status(),
    ).decode()

    assert "Cycle needs attention" in payload
    assert "Failed — see admin diagnostics" in payload
    assert "<code>rne</code>" in payload
    assert "<code>spain_spanish_news</code>" in payload
    assert "8 desired / unverified verified" in payload
    assert SECRET not in payload


def test_status_badge_escapes_untrusted_label() -> None:
    badge = status_badge('<unsafe data-title="secret">', tone="danger")

    assert 'class="badge badge-danger"' in badge
    assert "&lt;unsafe data-title=&quot;secret&quot;&gt;" in badge
    assert '<unsafe data-title="secret">' not in badge
