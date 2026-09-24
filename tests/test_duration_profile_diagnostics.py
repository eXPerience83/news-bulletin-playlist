from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.desired_state import (
    DURATION_WITHIN_DEFAULT_MAX,
    DurationEligibilityDecision,
)
from news_bulletin_playlist.diagnostics import DiagnosticEventStore, DiagnosticSeverity
from news_bulletin_playlist.duration_profile import duration_profile, profile_as_dict
from news_bulletin_playlist.engine import EngineCycleResult, PlaylistCycleOutcome
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics

NOW = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)


@dataclass
class _Runner:
    result: EngineCycleResult

    def run_cycle(self) -> EngineCycleResult:
        return self.result


def test_runtime_duration_profile_uses_shared_canonical_boundaries(tmp_path: Path) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    diagnostics = OperationalDiagnostics(store)
    durations = (599, 600, 899, 900, 1799, 1800, 3599, 3600)
    decisions = tuple(
        DurationEligibilityDecision(
            source_id=SourceId("rne"),
            source_native_id=f"private-{index}",
            duration_seconds=seconds,
            accepted=True,
            reason=DURATION_WITHIN_DEFAULT_MAX,
            max_seconds=7200,
        )
        for index, seconds in enumerate(durations)
    )
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        ok=True,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spain_spanish_news"),
                ok=True,
                desired_count=len(decisions),
                applied_count=len(decisions),
                wrote=False,
                last_success_at=NOW,
                duration_decisions=decisions,
                duration_unavailable_count=2,
            ),
        ),
    )

    InstrumentedEngineCycleRunner(_Runner(result), diagnostics, clock=lambda: NOW).run_cycle()

    events = store.list_events(limit=50)
    profile = next(event for event in events if event.event_name == "duration_profile")
    expected_buckets = profile_as_dict(duration_profile(durations))
    assert profile.details == {
        "sample_count": len(durations),
        "accepted_count": len(durations),
        "excluded_count": 0,
        **expected_buckets,
    }
    assert expected_buckets == {
        "under_10m": 1,
        "10_to_15m": 2,
        "15_to_30m": 2,
        "30_to_60m": 2,
        "60m_plus": 1,
    }
    assert sum(expected_buckets.values()) == profile.details["sample_count"]
    playlist_event = next(event for event in events if event.event_name == "playlist_reconciled")
    assert playlist_event.details["duration_unavailable_count"] == 2


def test_initialize_drops_only_legacy_duration_profiles_and_old_keys_are_not_writable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    store = DiagnosticEventStore(path)
    store.initialize()
    canonical_id = store.record(
        occurred_at=NOW,
        severity=DiagnosticSeverity.INFO,
        component="playlist.eligibility",
        event_name="duration_profile",
        details={
            "sample_count": 1,
            "accepted_count": 1,
            "excluded_count": 0,
            "under_10m": 1,
            "10_to_15m": 0,
            "15_to_30m": 0,
            "30_to_60m": 0,
            "60m_plus": 0,
        },
    )
    unrelated_id = store.record(
        occurred_at=NOW,
        severity=DiagnosticSeverity.INFO,
        component="engine",
        event_name="cycle_completed",
        details={"duration_ms": 1, "phase": "complete", "total": 1},
    )
    legacy_details = {
        "sample_count": 1,
        "accepted_count": 1,
        "excluded_count": 0,
        "lt_5m": 1,
        "m5_lt8": 0,
        "m8_lt10": 0,
        "m10_lt15": 0,
        "m15_lt20": 0,
        "m20_le30": 0,
        "gt_30m": 0,
    }
    with sqlite3.connect(path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO diagnostic_events (
                occurred_at, severity, component, event_name, cycle_id,
                source_id, playlist_id, details_json
            ) VALUES (?, 'INFO', 'playlist.eligibility', 'duration_profile', NULL, NULL, NULL, ?)
            """,
            (NOW.isoformat(), json.dumps(legacy_details, sort_keys=True, separators=(",", ":"))),
        )
        legacy_id = cursor.lastrowid

    store.initialize()

    events = store.list_events(limit=10)
    assert {event.event_id for event in events} == {canonical_id, unrelated_id}
    assert legacy_id not in {event.event_id for event in events}
    canonical = next(event for event in events if event.event_id == canonical_id)
    assert canonical.details["under_10m"] == 1

    with pytest.raises(ValueError, match="unknown key: lt_5m"):
        store.record(
            occurred_at=NOW,
            severity=DiagnosticSeverity.INFO,
            component="playlist.eligibility",
            event_name="duration_profile",
            details={"lt_5m": 1},
        )
