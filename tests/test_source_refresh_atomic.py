"""One source refresh must not split data/history from durable source authority."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest
from _spotify_catalogue import catalogue_page
from test_catalogue_validation_diagnostics import NOW, SOURCE, _Client, _item

import news_bulletin_playlist.spotify.first_persistence as persistence_module
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.first_models import RefreshReason
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal

TABLES = (
    "spotify_first_episode_state",
    "spotify_first_episode_history",
    "spotify_first_source_state",
)


def snapshot(items, *, now=NOW):
    return fetch_source_catalogue(
        _Client([catalogue_page(items)]), SOURCE, now=now, cutoff=now - timedelta(days=2)
    )


def apply(journal, value, *, now=NOW):
    return journal.apply_source_refresh(
        SOURCE,
        value,
        refreshed_at=now,
        reason=RefreshReason.CHANGE,
        detector_episode_id="D" * 22,
        coverage_cutoff_at=now - timedelta(days=2),
    )


def rows(path):
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1,2,3').fetchall()
            for table in TABLES
        }


def setup(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    apply(journal, snapshot([_item(i) for i in range(1, 5)]))
    return store, journal


def test_partial_merge_trust_history_restart_and_clean_recovery(tmp_path):
    store, journal = setup(tmp_path)
    later = NOW + timedelta(minutes=10)
    partial = snapshot([_item(9), _item(1), None, _item(4)], now=later)
    assert apply(journal, partial, now=later).removed == 0
    restarted = SpotifyFirstJournal(SQLiteStore(store.path))
    assert {row.spotify_episode_id for row in restarted.list_episodes()} == {
        f"{i:022d}" for i in (1, 2, 3, 4, 9)
    }
    state = restarted.get_source_state(SOURCE.id)
    assert state is not None
    assert not state.catalogue_complete and not state.coverage_proven
    assert state.api_episode_id is None and state.last_refresh_at == later
    history = restarted.list_episode_history(
        observed_since=NOW - timedelta(days=1), observed_until=later
    )
    assert {row.spotify_episode_id for row in history} == {f"{i:022d}" for i in (1, 2, 3, 4, 9)}
    assert len(history) == 5  # No null placeholder/history identity.
    clean_at = later + timedelta(minutes=10)
    clean = snapshot([_item(9)], now=clean_at)
    assert apply(restarted, clean, now=clean_at).removed == 4
    assert len(restarted.list_episodes()) == 1
    state = restarted.get_source_state(SOURCE.id)
    assert state is not None and state.catalogue_complete and state.coverage_proven
    assert state.api_episode_id == f"{9:022d}"
    assert (
        len(
            restarted.list_episode_history(
                observed_since=NOW - timedelta(days=1), observed_until=clean_at
            )
        )
        == 5
    )
    assert store.format_version() == 3


@pytest.mark.parametrize(
    "stage",
    [
        "before_episodes",
        "during_upsert",
        "before_history",
        "before_trust",
        "during_trust",
        "commit",
    ],
)
def test_partial_refresh_failure_rolls_back_all_rows_history_and_old_trust(
    tmp_path, monkeypatch, stage
):
    store, journal = setup(tmp_path)
    old = rows(store.path)
    later = NOW + timedelta(minutes=10)
    partial = snapshot([_item(9), _item(1), None, _item(4)], now=later)
    original_connect = sqlite3.connect

    class FaultConnection(sqlite3.Connection):
        operational_upserts = 0
        fired = False

        def execute(self, sql, parameters=(), /):
            if sql.startswith("\nINSERT INTO spotify_first_episode_state"):
                self.operational_upserts += 1
                if (
                    stage == "before_episodes"
                    or stage == "during_upsert"
                    and self.operational_upserts == 2
                ):
                    self.fired = True
                    raise sqlite3.OperationalError("injected episode failure")
            if (
                sql.startswith("\nINSERT INTO spotify_first_episode_history")
                and stage == "before_history"
            ):
                self.fired = True
                raise sqlite3.OperationalError("injected history failure")
            if "INSERT INTO spotify_first_source_state" in sql and stage in {
                "before_trust",
                "during_trust",
            }:
                if stage == "during_trust":
                    super().execute(sql, parameters)
                self.fired = True
                raise sqlite3.OperationalError("injected source trust failure")
            return super().execute(sql, parameters)

        def __exit__(self, exc_type, exc, tb):
            if stage == "commit" and exc_type is None:
                self.fired = True
                failure = sqlite3.OperationalError("injected commit failure")
                super().__exit__(type(failure), failure, None)  # SQLite rolls back the transaction.
                raise failure
            return super().__exit__(exc_type, exc, tb)

    connections = []

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=FaultConnection)
        connections.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(persistence_module.sqlite3, "connect", connect)
        with pytest.raises(PersistenceError):
            apply(journal, partial, now=later)
    assert len(connections) == 1 and connections[0].fired
    assert rows(store.path) == old
    restarted = SpotifyFirstJournal(SQLiteStore(store.path))
    state = restarted.get_source_state(SOURCE.id)
    assert state is not None and state.coverage_proven and state.last_refresh_at == NOW
    assert f"{9:022d}" not in {row.spotify_episode_id for row in restarted.list_episodes()}
    assert store.format_version() == 3


def test_older_refresh_cannot_merge_rows_with_newer_trust(tmp_path):
    store, journal = setup(tmp_path)
    old = rows(store.path)
    previous_at = NOW - timedelta(minutes=1)
    with pytest.raises(ValueError, match="predates"):
        apply(journal, snapshot([_item(9), None], now=previous_at), now=previous_at)
    assert rows(store.path) == old


def test_atomic_refresh_rejects_another_source_show_before_mutation(tmp_path):
    store, journal = setup(tmp_path)
    old = rows(store.path)
    value = snapshot([_item(9), None])
    wrong = replace(value.episodes[0], spotify_show_id="A" * 22)
    with pytest.raises(ValueError, match="another source/show"):
        apply(journal, replace(value, episodes=(wrong,)))
    assert rows(store.path) == old


def test_atomic_clean_removal_also_rolls_back_on_trust_failure(tmp_path, monkeypatch):
    store, journal = setup(tmp_path)
    before = rows(store.path)

    def fail(*args, **kwargs):
        raise PersistenceError("injected source trust failure after authoritative removal")

    monkeypatch.setattr(journal, "_record_refresh", fail)
    later = NOW + timedelta(minutes=10)
    with pytest.raises(PersistenceError):
        apply(journal, snapshot([_item(9)], now=later), now=later)
    assert rows(store.path) == before


def test_first_partial_refresh_failure_leaves_no_rows_or_source_authority(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()

    def fail(*args, **kwargs):
        raise PersistenceError("injected source trust failure on bootstrap")

    monkeypatch.setattr(journal, "_record_refresh", fail)
    with pytest.raises(PersistenceError):
        apply(journal, snapshot([_item(9), None]))
    assert rows(store.path) == {table: [] for table in TABLES}
