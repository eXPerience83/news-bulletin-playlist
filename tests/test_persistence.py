from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.models import CanonicalEdition, PlaylistId, SourceId
from news_bulletin_playlist.persistence import (
    DEFAULT_DB_FILENAME,
    LATEST_SCHEMA_VERSION,
    MatchStatus,
    PersistenceError,
    SQLiteStore,
)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "data" / DEFAULT_DB_FILENAME)
    store.initialize()
    return store


def _edition(
    native_id: str,
    *,
    source_id: str = "rne",
    published_at: datetime | None = None,
    edition_at: datetime | None = None,
) -> CanonicalEdition:
    published = published_at or datetime(2026, 8, 30, 9, 5, tzinfo=UTC)
    edition = edition_at or datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    return CanonicalEdition(
        source_id=SourceId(source_id),
        source_native_id=native_id,
        title="NOTICIAS RNE - 30.08.2026 - 11.00 H",
        published_at=published,
        edition_at=edition,
        duration_seconds=300,
    )


def _count_rows(path: Path, table: str) -> int:
    if table not in {"schema_migrations", "source_runs", "playlist_runs"}:
        raise ValueError("unexpected test table")
    with sqlite3.connect(path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_initialize_creates_temporary_data_database_and_is_repeatable(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = SQLiteStore(data_dir / DEFAULT_DB_FILENAME)

    store.initialize()
    store.initialize()

    assert data_dir.is_dir()
    assert store.path.is_file()
    assert store.schema_version() == LATEST_SCHEMA_VERSION
    assert _count_rows(store.path, "schema_migrations") == LATEST_SCHEMA_VERSION


def test_canonical_edition_survives_restart_and_upsert_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    edition = _edition("rne-asset-1")
    first_seen = datetime(2026, 8, 30, 9, 10, tzinfo=UTC)

    store.upsert_editions((edition,), observed_at=first_seen)
    store.upsert_editions((edition,), observed_at=first_seen + timedelta(minutes=5))

    restarted = SQLiteStore(store.path)
    restarted.initialize()

    assert restarted.get_edition(SourceId("rne"), "rne-asset-1") == edition
    assert restarted.list_editions() == (edition,)


def test_distinct_rne_identities_with_same_edition_time_can_coexist(tmp_path: Path) -> None:
    store = _store(tmp_path)
    edition_at = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    editions = (
        _edition("rne-asset-a", edition_at=edition_at),
        _edition("rne-asset-b", edition_at=edition_at),
    )

    store.upsert_editions(editions, observed_at=datetime(2026, 8, 30, 9, 10, tzinfo=UTC))

    loaded = store.list_editions(source_id=SourceId("rne"))
    assert {edition.source_native_id for edition in loaded} == {"rne-asset-a", "rne-asset-b"}
    assert {edition.edition_at for edition in loaded} == {edition_at}


def test_source_state_preserves_last_known_good_after_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    success_start = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    success_finish = success_start + timedelta(seconds=4)
    failure_start = success_start + timedelta(minutes=10)
    failure_finish = failure_start + timedelta(seconds=3)

    store.record_source_run(
        SourceId("ser"),
        started_at=success_start,
        finished_at=success_finish,
        ok=True,
        edition_count=12,
    )
    store.record_source_run(
        SourceId("ser"),
        started_at=failure_start,
        finished_at=failure_finish,
        ok=False,
        edition_count=0,
        error="HTTP 503",
    )
    store.record_source_run(
        SourceId("ser"),
        started_at=failure_start,
        finished_at=failure_finish,
        ok=False,
        edition_count=0,
        error="HTTP 503",
    )

    state = store.get_source_state(SourceId("ser"))
    assert state is not None
    assert state.last_attempt_at == failure_finish
    assert state.last_success_at == success_finish
    assert state.last_error == "HTTP 503"
    assert _count_rows(store.path, "source_runs") == 2


def test_spotify_match_mapping_survives_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    edition = _edition("rne-asset-1")
    now = datetime(2026, 8, 30, 9, 15, tzinfo=UTC)
    store.upsert_editions((edition,), observed_at=now)
    store.set_match_state(
        edition.source_id,
        edition.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:abc123",
        diagnostics="exact source-native match",
        updated_at=now,
    )

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    match = restarted.get_match_state(edition.source_id, edition.source_native_id)

    assert match is not None
    assert match.status is MatchStatus.MATCHED
    assert match.spotify_episode_uri == "spotify:episode:abc123"
    assert match.diagnostics == "exact source-native match"
    assert restarted.get_spotify_episode_uri(edition.source_id, edition.source_native_id) == (
        "spotify:episode:abc123"
    )


def test_match_state_requires_existing_canonical_edition(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(PersistenceError, match="set Spotify match state failed"):
        store.set_match_state(
            SourceId("rne"),
            "missing-native-id",
            status=MatchStatus.PENDING,
            updated_at=datetime(2026, 8, 30, 9, 15, tzinfo=UTC),
            diagnostics="not yet matched",
        )


def test_multi_playlist_runs_share_one_canonical_edition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    edition = _edition("rne-asset-1")
    now = datetime(2026, 8, 30, 9, 20, tzinfo=UTC)
    store.upsert_editions((edition,), observed_at=now)

    for offset, playlist in enumerate(("spain", "europe-spanish")):
        started = now + timedelta(minutes=offset)
        store.record_playlist_run(
            PlaylistId(playlist),
            started_at=started,
            finished_at=started + timedelta(seconds=2),
            ok=True,
            desired_count=1,
            applied_count=1,
        )

    assert store.list_editions() == (edition,)
    assert store.get_playlist_state(PlaylistId("spain")) is not None
    assert store.get_playlist_state(PlaylistId("europe-spanish")) is not None


def test_retention_prunes_only_old_operational_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    old_finish = now - timedelta(days=30, seconds=1)
    recent_finish = now - timedelta(days=10)
    edition = _edition("rne-asset-1", published_at=now - timedelta(days=40))
    store.upsert_editions((edition,), observed_at=now - timedelta(days=40))
    store.set_match_state(
        edition.source_id,
        edition.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:durable",
        updated_at=now - timedelta(days=40),
    )

    for source_id, finished in (("old", old_finish), ("recent", recent_finish)):
        store.record_source_run(
            SourceId(source_id),
            started_at=finished - timedelta(seconds=2),
            finished_at=finished,
            ok=True,
            edition_count=1,
        )
    for playlist_id, finished in (("old", old_finish), ("recent", recent_finish)):
        store.record_playlist_run(
            PlaylistId(playlist_id),
            started_at=finished - timedelta(seconds=2),
            finished_at=finished,
            ok=True,
            desired_count=1,
            applied_count=1,
        )

    result = store.prune_operational_history(now=now)

    assert result.source_runs_deleted == 1
    assert result.playlist_runs_deleted == 1
    assert _count_rows(store.path, "source_runs") == 1
    assert _count_rows(store.path, "playlist_runs") == 1
    assert store.get_edition(edition.source_id, edition.source_native_id) == edition
    assert store.get_spotify_episode_uri(edition.source_id, edition.source_native_id) == (
        "spotify:episode:durable"
    )
    assert store.prune_operational_history(now=now).source_runs_deleted == 0


def test_invalid_database_path_surfaces_actionable_persistence_error(tmp_path: Path) -> None:
    database_path = tmp_path / "database-as-directory"
    database_path.mkdir()
    store = SQLiteStore(database_path)

    with pytest.raises(PersistenceError, match="initialize database failed") as exc_info:
        store.initialize()

    assert str(database_path) in str(exc_info.value)
