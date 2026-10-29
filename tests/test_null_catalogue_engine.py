"""Real engine/journal/reconciler paths cannot publish unproven source evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from _spotify_catalogue import catalogue_page
from test_catalogue_validation_diagnostics import NOW, SOURCE, _item
from test_reconciliation import _desired, _FakeSpotify, _playlist
from test_source_refresh_atomic import rows

import news_bulletin_playlist.engine as engine_module
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import EngineConfig
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.reconciliation import SpotifyReconciliationError
from news_bulletin_playlist.spotify.client import SpotifyApiError
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.first_models import RefreshReason
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetectorCycleSummary,
    PublicDetectorJournal,
    spotify_show_id,
)
from news_bulletin_playlist.spotify_backoff import ProviderAuthorizationSession


class Auth:
    def get_access_token(self, *, now=None):
        return "secret-token"

    def authorization_context(self):
        return "test-authorization"


class Detector:
    def observe_sources(self, sources, *, observed_at):
        ids = tuple(source.id for source in sources)
        return PublicDetectorCycleSummary(len(ids), len(ids), 0, 0, changed_source_ids=ids)


class Client(_FakeSpotify):
    def __init__(self, catalogues, destinations=None):
        super().__init__(destinations or {})
        self.catalogues = catalogues
        self.calls = []

    def show_episodes(self, show_id, *, limit=50, offset=0):
        assert limit == 50
        self.calls.append((show_id, offset))
        items = self.catalogues[show_id]
        return catalogue_page(
            items[offset : offset + limit],
            offset=offset,
            total=len(items),
            next_url="next" if offset + limit < len(items) else None,
        )


def runner(tmp_path, client, *, sources=(SOURCE,), playlists=None):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    detector_journal = PublicDetectorJournal(store)
    detector_journal.initialize()
    playlists = playlists or (
        replace(_playlist("dependent", source=str(SOURCE.id)), max_episodes=2),
    )
    return EngineRunner(
        EngineConfig(sources=sources, playlists=playlists),
        store,
        Auth(),
        Detector(),
        detector_journal,
        journal,
        client_factory=lambda token: client,
        clock=lambda: NOW,
    )


def seed(engine, client):
    source = SOURCE
    old_at = NOW - timedelta(hours=1)
    value = fetch_source_catalogue(client, source, now=old_at, cutoff=old_at - timedelta(days=2))
    engine.first_journal.apply_source_refresh(
        source,
        value,
        refreshed_at=old_at,
        reason=RefreshReason.BOOTSTRAP,
        detector_episode_id=None,
        coverage_cutoff_at=old_at - timedelta(days=2),
    )


@pytest.mark.parametrize("previous", [False, True])
def test_null_refresh_persists_known_evidence_blocks_caps_and_recovers(tmp_path, previous):
    show = spotify_show_id(SOURCE)
    client = Client(
        {show: [_item(1), _item(2), _item(3)]}, {"dependent": [f"spotify:episode:{3:022d}"]}
    )
    engine = runner(tmp_path, client)
    if previous:
        seed(engine, client)
    client.catalogues[show] = [_item(9), _item(1), None, _item(3)]
    original_destination = list(client.states["dependent"])
    for cycle in range(3):
        engine.clock = lambda cycle=cycle: NOW + timedelta(minutes=10 * cycle)
        result = engine.run_cycle()
        assert not result.sources[0].ok and result.sources[0].refreshed
        assert result.sources[0].null_item_count == 1
        assert result.sources[0].error == "coverage_unproven_null_slot"
        assert not result.playlists[0].ok
        assert result.playlists[0].blocked_source_ids == (SOURCE.id,)
        assert result.playlists[0].block_reason == "coverage_unproven_null_slot"
        assert result.playlists[0].write_outcome == "not_attempted"
        assert client.writes == [] and client.reads == []
        assert client.states["dependent"] == original_destination
        state = engine.first_journal.get_source_state(SOURCE.id)
        assert state is not None and not state.coverage_proven and not state.catalogue_complete
        assert state.api_episode_id is None
        ids = {row.spotify_episode_id for row in engine.first_journal.list_episodes()}
        assert f"{9:022d}" in ids
        if previous:
            assert f"{2:022d}" in ids  # Could be the episode hidden by the unknown slot.
        assert not engine._playlist_sources_ready(engine.config.playlists[0], now=engine.clock())
    assert len(client.calls) == (4 if previous else 3)
    client.catalogues[show] = [_item(9)]
    engine.clock = lambda: NOW + timedelta(minutes=40)
    recovered = engine.run_cycle()
    assert recovered.sources[0].ok and recovered.playlists[0].ok
    assert recovered.playlists[0].blocked_source_ids == ()
    assert recovered.playlists[0].block_reason is None
    assert client.writes == [("dependent", [f"spotify:episode:{9:022d}"])]
    assert engine._playlist_sources_ready(engine.config.playlists[0], now=engine.clock())
    assert {row.spotify_episode_id for row in engine.first_journal.list_episodes()} == {f"{9:022d}"}


def test_rne_shaped_nineteen_slot_catalogue_blocks_without_mutating(tmp_path):
    show = spotify_show_id(SOURCE)
    items = [_item(number) for number in range(1, 20)]
    items[17] = None  # The observed RNE shape: one-based provider position 18.
    client = Client({show: items}, {"dependent": [f"spotify:episode:{1:022d}"]})
    engine = runner(tmp_path, client)
    outcome = engine.run_cycle()
    assert client.calls == [(show, 0)]
    assert outcome.sources[0].episode_count == 18
    assert outcome.sources[0].null_item_count == 1
    assert outcome.playlists[0].blocked_source_ids == (SOURCE.id,)
    assert outcome.playlists[0].block_reason == "coverage_unproven_null_slot"
    assert not outcome.playlists[0].ok
    assert not client.reads and not client.writes
    assert client.states["dependent"] == [f"spotify:episode:{1:022d}"]


def test_shared_unproven_source_blocks_both_destinations_but_not_independent(tmp_path):
    independent = BUILTIN_CATALOG.sources[1]
    show = spotify_show_id(SOURCE)
    other_show = spotify_show_id(independent)
    playlists = tuple(
        replace(_playlist(name, source=str(source.id)), max_episodes=2)
        for name, source in (
            ("dependent-a", SOURCE),
            ("dependent-b", SOURCE),
            ("independent", independent),
        )
    )
    client = Client({show: [_item(9), None], other_show: [_item(1)]})
    engine = runner(tmp_path, client, sources=(SOURCE, independent), playlists=playlists)
    result = engine.run_cycle()
    assert [outcome.ok for outcome in result.playlists] == [False, False, True]
    assert [outcome.blocked_source_ids for outcome in result.playlists] == [
        (SOURCE.id,),
        (SOURCE.id,),
        (),
    ]
    assert client.writes == [("independent", [f"spotify:episode:{1:022d}"])]
    assert client.calls == [(show, 0), (other_show, 0)]
    assert all(read[0] == "independent" for read in client.reads)


def test_original_split_commit_failure_window_is_closed_in_real_engine(tmp_path, monkeypatch):
    show = spotify_show_id(SOURCE)
    client = Client({show: [_item(1), _item(2), _item(3)]})
    engine = runner(tmp_path, client)
    seed(engine, client)
    before = rows(engine.store.path)
    client.catalogues[show] = [_item(9), _item(1), None, _item(3)]

    def fail(*args, **kwargs):
        raise PersistenceError("injected failure after source episodes/history before trust")

    monkeypatch.setattr(engine.first_journal, "_record_refresh", fail)
    result = engine.run_cycle()
    assert not result.sources[0].refreshed
    assert rows(engine.store.path) == before
    assert all(f"spotify:episode:{9:022d}" not in uris for _, uris in client.writes)
    assert engine.first_journal.get_source_state(SOURCE.id).coverage_proven


def test_stale_escalation_null_demotes_authority_and_does_not_retry_write(tmp_path):
    show = spotify_show_id(SOURCE)
    client = Client({show: [_item(1), _item(2), _item(3)]})
    engine = runner(tmp_path, client)
    seed(engine, client)
    client.catalogues[show] = [_item(9), None]
    playlist = engine.config.playlists[0]
    exc = SpotifyReconciliationError("stale desired item")
    exc.__cause__ = SpotifyApiError(400, "secret-provider-body")
    result = engine._stale_rejection_retry(
        playlist,
        _desired(playlist, f"spotify:episode:{3:022d}"),
        client,
        ProviderAuthorizationSession("secret-token", "test-authorization"),
        exc,
        now=NOW,
    )
    assert result is None and client.writes == []
    assert not engine.first_journal.get_source_state(SOURCE.id).coverage_proven
    assert not engine._playlist_sources_ready(playlist, now=NOW)
    assert {row.spotify_episode_id for row in engine.first_journal.list_episodes()} == {
        f"{i:022d}" for i in (1, 2, 3, 9)
    }


@pytest.mark.parametrize("verification", ["exact", "pending", "degraded"])
def test_destination_verification_cannot_bypass_unproven_source(
    tmp_path, monkeypatch, verification
):
    from news_bulletin_playlist.snapshot_pending import (
        PendingSnapshotConfirmation,
        PendingSnapshotJournal,
    )
    from news_bulletin_playlist.spotify.first_desired import desired_fingerprint

    show = spotify_show_id(SOURCE)
    client = Client({show: [_item(9), None]})
    engine = runner(tmp_path, client)
    playlist = engine.config.playlists[0]
    fingerprint = desired_fingerprint((f"spotify:episode:{9:022d}",))
    if verification == "pending":
        pending = PendingSnapshotJournal(engine.store)
        pending.set(
            PendingSnapshotConfirmation(
                playlist.id,
                playlist.destination.external_id,
                "before",
                "expected",
                fingerprint,
                NOW,
                NOW + timedelta(hours=1),
                True,
            )
        )
    elif verification == "exact":
        engine.first_journal.record_destination_verification(
            playlist.id,
            destination_id=playlist.destination.external_id,
            desired_fingerprint=fingerprint,
            verified_at=NOW,
        )
    else:
        engine.first_journal.record_degraded_destination_verification(
            playlist.id,
            destination_id=playlist.destination.external_id,
            desired_fingerprint=fingerprint,
            verified_at=NOW,
        )

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "unproven source must block before destination verification/reconciliation"
        )

    monkeypatch.setattr(engine, "_destination_skip_evidence", forbidden)
    result = engine.run_cycle()
    assert not result.playlists[0].ok
    assert client.writes == [] and client.reads == []
    if verification == "pending":
        assert pending.get(playlist.id) is not None


def test_regular_and_stale_paths_use_one_catalogue_refresh_helper(tmp_path, monkeypatch):
    show = spotify_show_id(SOURCE)
    client = Client({show: [_item(1), _item(2)]})
    engine = runner(tmp_path, client)
    applied = []
    original_apply = engine._apply_source_catalogue_refresh

    def record_apply(source, snapshot, **kwargs):
        applied.append((kwargs["reason"], snapshot.null_item_count))
        return original_apply(source, snapshot, **kwargs)

    monkeypatch.setattr(engine, "_apply_source_catalogue_refresh", record_apply)

    initial = engine.run_cycle()
    assert initial.sources[0].ok
    assert applied == [(RefreshReason.BOOTSTRAP, 0)]
    writes_before = list(client.writes)

    client.catalogues[show] = [_item(9), None]
    playlist = engine.config.playlists[0]
    exc = SpotifyReconciliationError("stale desired item")
    exc.__cause__ = SpotifyApiError(400, "secret-provider-body")
    retry = engine._stale_rejection_retry(
        playlist,
        _desired(playlist, f"spotify:episode:{1:022d}"),
        client,
        ProviderAuthorizationSession("secret-token", "test-authorization"),
        exc,
        now=NOW + timedelta(minutes=10),
    )

    assert retry is None
    assert applied == [
        (RefreshReason.BOOTSTRAP, 0),
        (RefreshReason.STALE_ESCALATION, 1),
    ]
    assert client.writes == writes_before
    state = engine.first_journal.get_source_state(SOURCE.id)
    assert state is not None and not state.coverage_proven
    assert state.api_episode_id is None


@pytest.mark.parametrize("has_null", [False, True])
@pytest.mark.parametrize("fail_commit", [False, True])
def test_shared_refresh_helper_orders_warning_commit_and_detector_marker(
    tmp_path, monkeypatch, has_null, fail_commit
):
    show = spotify_show_id(SOURCE)
    items = [_item(1), None] if has_null else [_item(1)]
    client = Client({show: items})
    engine = runner(tmp_path, client)
    cutoff = NOW - timedelta(days=2)
    snapshot = fetch_source_catalogue(client, SOURCE, now=NOW, cutoff=cutoff)
    events = []
    original_apply = engine.first_journal.apply_source_refresh
    original_mark = engine.detector_journal.mark_catalogue_refresh

    def warn(*args, **kwargs):
        assert kwargs["null_item_count"] == 1
        events.append("warning")

    def apply(*args, **kwargs):
        events.append("persist")
        if fail_commit:
            raise PersistenceError("injected source refresh failure")
        return original_apply(*args, **kwargs)

    def mark(*args, **kwargs):
        events.append("marker")
        return original_mark(*args, **kwargs)

    monkeypatch.setattr(engine_module, "emit_catalogue_null_slots", warn)
    monkeypatch.setattr(engine.first_journal, "apply_source_refresh", apply)
    monkeypatch.setattr(engine.detector_journal, "mark_catalogue_refresh", mark)

    if fail_commit:
        with pytest.raises(PersistenceError, match="injected source refresh failure"):
            engine._apply_source_catalogue_refresh(
                SOURCE,
                snapshot,
                now=NOW,
                reason=RefreshReason.CHANGE,
                detector_episode_id=None,
                cutoff=cutoff,
            )
    else:
        removed = engine._apply_source_catalogue_refresh(
            SOURCE,
            snapshot,
            now=NOW,
            reason=RefreshReason.CHANGE,
            detector_episode_id=None,
            cutoff=cutoff,
        )
        assert removed == 0

    assert events == (
        (["warning"] if has_null else []) + ["persist"] + ([] if fail_commit else ["marker"])
    )
    state = engine.first_journal.get_source_state(SOURCE.id)
    if fail_commit:
        assert state is None
    else:
        assert state is not None and state.coverage_proven is not has_null
