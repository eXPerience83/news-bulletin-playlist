from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_bulletin_playlist.models import CanonicalEdition, SourceId
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.production_diagnostic_queries import (
    read_recovery_diagnostic_snapshot,
)
from news_bulletin_playlist.spotify_planner import SpotifyPlannerJournal

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
URI = "spotify:episode:0000000000000000000001"


def _edition(native_id: str, *, age_hours: int = 1) -> CanonicalEdition:
    published = NOW - timedelta(hours=age_hours)
    return CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id=native_id,
        title=f"SER {native_id}",
        published_at=published,
        edition_at=published,
        duration_seconds=60,
    )


def _matched(store: SQLiteStore, edition: CanonicalEdition) -> None:
    assert store.set_match_state(
        edition.source_id,
        edition.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=URI,
        spotify_duration_seconds=60,
        diagnostics="matched",
        updated_at=NOW,
    )


def test_snapshot_batches_barriers_and_recovery_relevant_rows(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    journal = SpotifyPlannerJournal(store)
    tracked = _edition("tracked")
    untracked = _edition("untracked")
    has_input = _edition("has-input")
    outside_window = _edition("old", age_hours=72)
    store.upsert_editions(
        (tracked, untracked, has_input, outside_window),
        observed_at=NOW,
    )
    for edition in (tracked, untracked, has_input, outside_window):
        _matched(store, edition)

    journal.record_matched_recovery(
        source_id="ser",
        source_native_id=tracked.source_native_id,
        input_fingerprint="a" * 64,
        attempted_at=NOW,
    )
    journal.observe_match_input(
        source_id="ser",
        source_native_id=has_input.source_native_id,
        input_fingerprint="b" * 64,
        observed_at=NOW,
    )
    durable = store.get_match_state(tracked.source_id, tracked.source_native_id)
    assert durable is not None
    journal.record_persistence_rejection_barrier(durable, rejected_at=NOW)

    snapshot = read_recovery_diagnostic_snapshot(
        store.path,
        SourceId("ser"),
        matching_cutoff=NOW - timedelta(hours=48),
        observed_at=NOW,
    )

    assert snapshot.active_persistence_rejection_barrier_count == 1
    by_id = {row.edition.source_native_id: row for row in snapshot.matched_without_input}
    assert set(by_id) == {"tracked", "untracked"}
    assert by_id["tracked"].recovery is not None
    assert by_id["tracked"].recovery.input_fingerprint == "a" * 64
    assert by_id["untracked"].recovery is None


def test_snapshot_ignores_stale_barrier_after_durable_match_changes(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    journal = SpotifyPlannerJournal(store)
    edition = _edition("barrier")
    store.upsert_editions((edition,), observed_at=NOW)
    _matched(store, edition)
    durable = store.get_match_state(edition.source_id, edition.source_native_id)
    assert durable is not None
    journal.record_persistence_rejection_barrier(durable, rejected_at=NOW)

    assert store.set_match_state(
        edition.source_id,
        edition.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:0000000000000000000002",
        spotify_duration_seconds=61,
        diagnostics="replacement",
        updated_at=NOW + timedelta(minutes=1),
    )

    snapshot = read_recovery_diagnostic_snapshot(
        store.path,
        SourceId("ser"),
        matching_cutoff=NOW - timedelta(hours=48),
        observed_at=NOW + timedelta(minutes=2),
    )

    assert snapshot.active_persistence_rejection_barrier_count == 0


def test_snapshot_keeps_exact_matching_window_boundaries(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    cutoff = NOW - timedelta(hours=48)
    at_cutoff = CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id="cutoff",
        title="SER cutoff",
        published_at=cutoff,
        edition_at=cutoff,
        duration_seconds=60,
    )
    at_now = CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id="now",
        title="SER now",
        published_at=NOW,
        edition_at=NOW,
        duration_seconds=60,
    )
    before = CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id="before",
        title="SER before",
        published_at=cutoff - timedelta(microseconds=1),
        edition_at=cutoff - timedelta(microseconds=1),
        duration_seconds=60,
    )
    future = CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id="future",
        title="SER future",
        published_at=NOW + timedelta(microseconds=1),
        edition_at=NOW + timedelta(microseconds=1),
        duration_seconds=60,
    )
    store.upsert_editions((at_cutoff, at_now, before, future), observed_at=NOW)
    for edition in (at_cutoff, at_now, before, future):
        _matched(store, edition)

    snapshot = read_recovery_diagnostic_snapshot(
        store.path,
        SourceId("ser"),
        matching_cutoff=cutoff,
        observed_at=NOW,
    )

    assert {row.edition.source_native_id for row in snapshot.matched_without_input} == {
        "cutoff",
        "now",
    }
