from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_state import (
    ManagedState,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.first_desired import build_spotify_first_desired_state
from news_bulletin_playlist.spotify.first_models import SpotifyFirstEpisode, TimestampSource
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import spotify_show_id

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
SOURCE = SourceId("ser")
SHOW = "A" * 22


def _journal(tmp_path: Path) -> SpotifyFirstJournal:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    return journal


def _episode(
    letter: str,
    *,
    duration: int | None = 305,
    seen: datetime = NOW,
    show_id: str = SHOW,
) -> SpotifyFirstEpisode:
    episode_id = letter * 22
    return SpotifyFirstEpisode(
        source_id=SOURCE,
        spotify_show_id=show_id,
        spotify_episode_id=episode_id,
        spotify_episode_uri=f"spotify:episode:{episode_id}",
        title="Accepted bulletin",
        edition_at=None,
        timestamp_source=TimestampSource.FIRST_SEEN,
        release_date_raw="2026-09-24",
        release_date_precision="day",
        duration_seconds=duration,
        first_seen_at=seen,
        last_seen_at=seen,
    )


def _history(journal: SpotifyFirstJournal, *, source_id: SourceId | None = None, limit: int = 500):
    return journal.list_episode_history(
        observed_since=NOW - timedelta(days=60),
        observed_until=NOW + timedelta(days=2),
        source_id=source_id,
        limit=limit,
    )


def test_exact_and_missing_durations_and_repeated_observations(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.replace_source_episodes(
        SOURCE, (_episode("B"), _episode("C", duration=None)), coverage_proven=True
    )
    first = _history(journal)
    assert {row.spotify_episode_id: row.duration_seconds for row in first} == {
        "B" * 22: 305,
        "C" * 22: None,
    }

    later = NOW + timedelta(hours=1)
    journal.replace_source_episodes(
        SOURCE, (_episode("B", duration=None, seen=later),), coverage_proven=False
    )
    assert _history(journal)[0].duration_seconds == 305
    updated = _episode("B", duration=317, seen=later + timedelta(hours=1))
    journal.replace_source_episodes(SOURCE, (updated,), coverage_proven=False)
    journal.replace_source_episodes(SOURCE, (updated,), coverage_proven=False)
    rows = _history(journal)
    assert len(rows) == 2
    row = next(item for item in rows if item.spotify_episode_id == "B" * 22)
    assert row.duration_seconds == 317
    assert row.first_seen_at == NOW
    assert row.last_seen_at == later + timedelta(hours=1)
    # A late-arriving older observation cannot overwrite newer evidence.
    journal.replace_source_episodes(
        SOURCE, (replace(_episode("B", duration=100), title="Old"),), coverage_proven=False
    )
    assert (
        next(item for item in _history(journal) if item.spotify_episode_id == "B" * 22).title
        == "Accepted bulletin"
    )


def test_covered_replacement_preserves_history_not_desired(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    source = next(source for source in BUILTIN_CATALOG.sources if source.id == SOURCE)
    show_id = spotify_show_id(source)
    journal.replace_source_episodes(
        SOURCE,
        (_episode("B", show_id=show_id), _episode("C", show_id=show_id)),
        coverage_proven=True,
    )
    counts = journal.replace_source_episodes(
        SOURCE,
        (_episode("C", show_id=show_id), _episode("D", show_id=show_id)),
        coverage_proven=True,
    )
    assert counts.removed == 1
    assert {row.spotify_episode_id for row in journal.list_episodes()} == {"C" * 22, "D" * 22}
    assert {row.spotify_episode_id for row in _history(journal)} == {"B" * 22, "C" * 22, "D" * 22}

    # The desired builder consumes only the operational API; history-only B cannot reappear.
    playlist = replace(
        activate_template(BUILTIN_CATALOG.playlist("spain_spanish_news"), "destination"),
        source_ids=(SOURCE,),
    )
    config = compile_engine_config(BUILTIN_CATALOG, ManagedState(playlists=(playlist,)))
    current = journal.list_episodes()
    build = build_spotify_first_desired_state(
        config.playlists[0], current, sources={SOURCE: source}, now=NOW
    )
    assert f"spotify:episode:{'B' * 22}" not in build.desired.uris
    assert set(build.desired.uris) == {
        f"spotify:episode:{'C' * 22}",
        f"spotify:episode:{'D' * 22}",
    }


def test_repeated_covered_snapshots_do_not_duplicate_history(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    for offset in range(4):
        observed = NOW + timedelta(minutes=offset)
        journal.replace_source_episodes(
            SOURCE,
            (_episode("B", seen=observed),),
            coverage_proven=True,
        )
    assert len(_history(journal)) == 1
    assert _history(journal)[0].first_seen_at == NOW
    assert _history(journal)[0].last_seen_at == NOW + timedelta(minutes=3)


def test_history_upsert_failure_rolls_back_operational_replacement(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.replace_source_episodes(SOURCE, (_episode("B"),), coverage_proven=True)
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_history_insert BEFORE INSERT ON "
            "spotify_first_episode_history BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    with pytest.raises(PersistenceError, match="replace Spotify-first source catalogue failed"):
        journal.replace_source_episodes(SOURCE, (_episode("C"),), coverage_proven=True)
    assert {row.spotify_episode_id for row in journal.list_episodes()} == {"B" * 22}
    assert {row.spotify_episode_id for row in _history(journal)} == {"B" * 22}


def test_history_and_current_prune_are_independent_with_exact_cutoff(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    cutoff = NOW - timedelta(days=30)
    old = cutoff - timedelta(microseconds=1)
    journal.replace_source_episodes(
        SOURCE,
        (
            _episode("B", seen=old),
            _episode("C", seen=cutoff),
            _episode("D", seen=cutoff + timedelta(microseconds=1)),
        ),
        coverage_proven=True,
    )
    assert journal.prune_history(now=NOW) == 1
    assert {row.spotify_episode_id for row in _history(journal)} == {"C" * 22, "D" * 22}
    assert {row.spotify_episode_id for row in journal.list_episodes()} == {
        "B" * 22,
        "C" * 22,
        "D" * 22,
    }
    assert journal.prune(before=cutoff + timedelta(microseconds=1)) == 2
    assert {row.spotify_episode_id for row in journal.list_episodes()} == {"D" * 22}
    assert {row.spotify_episode_id for row in _history(journal)} == {"C" * 22, "D" * 22}


def test_history_read_is_filtered_bounded_and_deterministic(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.replace_source_episodes(
        SOURCE, (_episode("B"), _episode("C"), _episode("D")), coverage_proven=True
    )
    other = replace(_episode("E"), source_id=SourceId("other"))
    journal.replace_source_episodes(other.source_id, (other,), coverage_proven=True)
    assert [row.spotify_episode_id for row in _history(journal, source_id=SOURCE, limit=2)] == [
        "B" * 22,
        "C" * 22,
    ]
    assert len(_history(journal)) == 4
    assert (
        journal.list_episode_history(
            observed_since=NOW + timedelta(seconds=1),
            observed_until=NOW + timedelta(days=1),
        )
        == ()
    )
    for limit in (0, 1001, True):
        with pytest.raises(ValueError, match="limit"):
            _history(journal, limit=limit)
    with pytest.raises(ValueError, match="precedes"):
        journal.list_episode_history(observed_since=NOW, observed_until=NOW - timedelta(seconds=1))


def test_history_schema_is_allowlisted(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.replace_source_episodes(SOURCE, (_episode("B"),), coverage_proven=True)
    with sqlite3.connect(journal.path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(spotify_first_episode_history)")
        }
        values = connection.execute("SELECT * FROM spotify_first_episode_history").fetchall()
    assert columns == {
        "source_id",
        "spotify_show_id",
        "spotify_episode_id",
        "spotify_episode_uri",
        "title",
        "edition_at",
        "timestamp_source",
        "release_date_raw",
        "release_date_precision",
        "duration_seconds",
        "first_seen_at",
        "last_seen_at",
    }
    assert len(values) == 1


class _CatalogueClient:
    def __init__(self, page: dict[str, Any]) -> None:
        self.page = page

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        assert offset == 0 and limit == 50 and show_id
        return self.page


def test_shipped_filter_rejected_episode_never_reaches_history(tmp_path: Path) -> None:
    source = next(source for source in BUILTIN_CATALOG.sources if source.collection_filter_id)
    rejected_id, accepted_id = "B" * 22, "C" * 22

    def item(episode_id: str, title: str) -> dict[str, Any]:
        return {
            "id": episode_id,
            "uri": f"spotify:episode:{episode_id}",
            "name": title,
            "release_date": "2026-09-24",
            "release_date_precision": "day",
            "duration_ms": 305_000,
            "secret_unknown": "secret-provider-body",
            "access_token": "secret-oauth-token",
            "public_capture": "secret-public-body",
            "headers": {"Authorization": "secret-auth-header", "cookie": "secret-cookie"},
            "pkce": "secret-PKCE",
        }

    snapshot = fetch_source_catalogue(
        _CatalogueClient(
            catalogue_page(
                [
                    item(rejected_id, "Other product"),
                    item(accepted_id, "La ONU en Minutos 24 de septiembre de 2026"),
                ],
                total=2,
            )
        ),
        source,
        now=NOW,
        cutoff=NOW - timedelta(hours=48),
    )
    assert snapshot.filter_rejected_count == 1
    assert snapshot.evidence == ()
    assert [row.spotify_episode_id for row in snapshot.episodes] == [accepted_id]
    journal = _journal(tmp_path)
    journal.replace_source_episodes(
        source.id, snapshot.episodes, coverage_proven=snapshot.coverage_proven
    )
    assert [row.spotify_episode_id for row in _history(journal)] == [accepted_id]
    with sqlite3.connect(journal.path) as connection:
        stored = repr(connection.execute("SELECT * FROM spotify_first_episode_history").fetchall())
    for secret in (
        "secret-provider-body",
        "secret-oauth-token",
        "secret-public-body",
        "secret-auth-header",
        "secret-cookie",
        "secret-PKCE",
    ):
        assert secret not in stored
