from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_bulletin_playlist.models import CanonicalEdition, SourceId
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.spotify_planner import SpotifyPlannerJournal


def _edition(
    native_id: str,
    published_at: datetime,
    *,
    edition_at: datetime | None = None,
) -> CanonicalEdition:
    return CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id=native_id,
        title=f"bulletin {native_id}",
        published_at=published_at,
        edition_at=published_at if edition_at is None else edition_at,
    )


def _last_seen_at(store: SQLiteStore, native_id: str) -> datetime:
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            """
            SELECT last_seen_at
            FROM canonical_editions
            WHERE source_id = 'ser' AND source_native_id = ?
            """,
            (native_id,),
        ).fetchone()
    assert row is not None
    return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).astimezone(UTC)


def test_canonical_retention_uses_last_seen_not_provider_timestamps(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "data" / "state.sqlite3")
    store.initialize()
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    observed_today = _edition(
        "observed-today",
        now - timedelta(days=40),
        edition_at=now,
    )
    stale = _edition("stale", now - timedelta(days=40))
    store.upsert_editions((observed_today,), observed_at=now)
    store.upsert_editions((stale,), observed_at=now - timedelta(days=31))
    store.set_match_state(
        stale.source_id,
        stale.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:stale",
        updated_at=now - timedelta(days=31),
    )

    result = store.prune_operational_history(now=now, protected_identities=())

    assert result.canonical_editions_deleted == 1
    assert (
        store.get_edition(observed_today.source_id, observed_today.source_native_id)
        == observed_today
    )
    assert store.get_edition(stale.source_id, stale.source_native_id) is None
    assert store.get_spotify_episode_uri(stale.source_id, stale.source_native_id) is None


def test_reobserving_asset_refreshes_last_seen_even_when_metadata_changes(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "data" / "state.sqlite3")
    store.initialize()
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    first_observation = now - timedelta(days=31)
    original = _edition("asset", now, edition_at=now)
    corrected = CanonicalEdition(
        source_id=original.source_id,
        source_native_id=original.source_native_id,
        title="corrected title",
        published_at=now - timedelta(days=45),
        edition_at=now - timedelta(minutes=5),
        duration_seconds=251,
    )
    store.upsert_editions((original,), observed_at=first_observation)
    store.upsert_editions((corrected,), observed_at=now)

    assert _last_seen_at(store, "asset") == now
    assert store.get_edition(SourceId("ser"), "asset") == corrected
    assert (
        store.prune_operational_history(now=now, protected_identities=()).canonical_editions_deleted
        == 0
    )


def test_canonical_retention_exact_cutoff_is_not_stale_until_older(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "data" / "state.sqlite3")
    store.initialize()
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    cutoff = now - timedelta(days=30)
    boundary = _edition("boundary", now - timedelta(days=90))
    older = _edition("older", now + timedelta(days=5))
    store.upsert_editions((boundary,), observed_at=cutoff)
    store.upsert_editions((older,), observed_at=cutoff - timedelta(microseconds=1))

    result = store.prune_operational_history(now=now, protected_identities=())

    assert result.canonical_editions_deleted == 1
    assert store.get_edition(SourceId("ser"), "boundary") == boundary
    assert store.get_edition(SourceId("ser"), "older") is None


def test_canonical_retention_requires_explicit_protection_set(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "data" / "state.sqlite3")
    store.initialize()
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    stale = _edition("stale", now - timedelta(days=40))
    store.upsert_editions((stale,), observed_at=now - timedelta(days=40))

    safe_default = store.prune_operational_history(now=now)

    assert safe_default.canonical_editions_deleted == 0
    assert store.get_edition(stale.source_id, stale.source_native_id) == stale


def test_v6_seeded_barrier_survives_old_pubdate_prune_and_restart(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "data" / "state.sqlite3")
    store.initialize()
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    edition = _edition(
        "migration-barrier",
        now - timedelta(days=31),
        edition_at=now,
    )
    store.upsert_editions((edition,), observed_at=now)
    assert store.set_match_state(
        edition.source_id,
        edition.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:" + "a" * 22,
        spotify_duration_seconds=60,
        diagnostics="durable mapping ahead of rollback",
        updated_at=now + timedelta(hours=4),
    )
    journal = SpotifyPlannerJournal(store)
    journal.record_matched_recovery(
        source_id=str(edition.source_id),
        source_native_id=edition.source_native_id,
        input_fingerprint="a" * 64,
        attempted_at=now,
    )

    # Recreate the exact production upgrade boundary: a v6 database with an implicit
    # rejection barrier, then initialize v7 and let its migration reconstruct that barrier.
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE spotify_planner_persistence_rejection_state")
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")

    upgraded = SQLiteStore(store.path)
    assert upgraded.schema_version() == 6
    upgraded.initialize()
    restarted_journal = SpotifyPlannerJournal(SQLiteStore(store.path))
    barrier = restarted_journal.get_persistence_rejection_barrier(
        str(edition.source_id), edition.source_native_id
    )
    durable = upgraded.get_match_state(edition.source_id, edition.source_native_id)
    assert barrier is not None
    assert barrier.protects(durable)

    # Provider pubDate is older than 30 days, but local observation is current. Prune must
    # preserve the canonical parent, so the reconstructed v7 barrier survives restart too.
    result = upgraded.prune_operational_history(now=now, protected_identities=())
    assert result.canonical_editions_deleted == 0
    assert upgraded.get_edition(edition.source_id, edition.source_native_id) == edition
    assert upgraded.get_match_state(edition.source_id, edition.source_native_id) == durable
    assert (
        SpotifyPlannerJournal(SQLiteStore(store.path)).get_persistence_rejection_barrier(
            str(edition.source_id), edition.source_native_id
        )
        == barrier
    )


def test_clock_rollback_observation_cannot_make_canonical_prematurely_stale(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "data" / "state.sqlite3")
    store.initialize()
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    original = _edition("rollback-observation", now, edition_at=now)
    corrected_during_rollback = CanonicalEdition(
        source_id=original.source_id,
        source_native_id=original.source_native_id,
        title="rollback metadata",
        published_at=now - timedelta(days=45),
        edition_at=now - timedelta(days=45),
    )
    store.upsert_editions((original,), observed_at=now)
    store.upsert_editions(
        (corrected_during_rollback,),
        observed_at=now - timedelta(hours=5),
    )

    # Monotonic observation memory wins during a clock rollback. Older observations cannot
    # decrease last_seen_at or overwrite newer canonical metadata.
    assert _last_seen_at(store, original.source_native_id) == now
    assert store.get_edition(original.source_id, original.source_native_id) == original

    exact_boundary = now + timedelta(days=30)
    assert (
        store.prune_operational_history(
            now=exact_boundary,
            protected_identities=(),
        ).canonical_editions_deleted
        == 0
    )
    assert (
        store.prune_operational_history(
            now=exact_boundary + timedelta(microseconds=1),
            protected_identities=(),
        ).canonical_editions_deleted
        == 1
    )
