from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from _spotify_catalogue import catalogue_page
from test_engine import (
    NOW,
    SER_TITLE,
    SER_URI,
    _Auth,
    _config,
    _playlist,
    _rss,
    _rss_two,
    _source,
    _Spotify,
    _store,
)
from test_shadow_planner_review import setup
from test_spotify_matcher import FakeCatalogClient, _candidate, _edition
from test_spotify_matcher import _source as _matcher_source

from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.persistence import MatchStatus
from news_bulletin_playlist.reconciliation import build_desired_state_from_store
from news_bulletin_playlist.soft_retention_engine import resolve_soft_retention_evidence
from news_bulletin_playlist.spotify.matcher import MatchEvaluationIntent, match_source_editions

REPLACEMENT_URI = "spotify:episode:" + "9" * 22


class _RollbackReplacementSpotify(_Spotify):
    replacement_catalogue = False
    include_invalid_cached = False

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not self.replacement_catalogue:
            return super().show_episodes(show_id, limit=limit, offset=offset)
        assert show_id == "ser-show"
        assert limit == 50
        assert offset in {0, 50}
        self.show_calls += 1
        if offset == 0:
            items = [
                {
                    "uri": f"spotify:episode:{index + 1000:022d}",
                    "name": "unrelated",
                    "release_date": "2026-08-30",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
                for index in range(50)
            ]
            if self.include_invalid_cached:
                items[0] = {
                    "uri": SER_URI,
                    "name": "contenido que no representa la edición",
                    "release_date": "2026-08-30",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
            return catalogue_page(
                items,
                offset=offset,
                limit=limit,
                next_url="more",
                total=51,
            )
        return catalogue_page(
            [
                {
                    "uri": REPLACEMENT_URI,
                    "name": SER_TITLE,
                    "release_date": "2026-08-30",
                    "release_date_precision": "day",
                    "duration_ms": 61_000,
                }
            ],
            offset=offset,
            limit=limit,
            total=51,
        )


def _pin_durable_match_ahead(runner: EngineRunner) -> None:
    state = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert state is not None and state.status is MatchStatus.MATCHED
    assert state.spotify_episode_uri == SER_URI
    assert runner.store.set_match_state(
        SourceId("ser"),
        "ser-1",
        status=MatchStatus.MATCHED,
        spotify_episode_uri=SER_URI,
        spotify_duration_seconds=60,
        diagnostics=state.diagnostics,
        updated_at=NOW + timedelta(hours=4),
    )


@pytest.mark.parametrize("include_invalid_cached", [False, True])
def test_rejected_repair_blocks_destination_and_uses_durable_recovery_backoff(
    tmp_path: Path,
    include_invalid_cached: bool,
) -> None:
    clock = [NOW]
    spotify = _RollbackReplacementSpotify()
    store = _store(tmp_path)
    runner = EngineRunner(
        _config("first"),
        store,
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    assert spotify.items["destination-first"] == [SER_URI]
    _pin_durable_match_ahead(runner)
    runner.planner_journal.clear_match_input("ser", "ser-1")
    runner.planner_journal.mark_destination_unknown("first")
    spotify.items["destination-first"] = []
    spotify.replacement_catalogue = True
    spotify.include_invalid_cached = include_invalid_cached
    writes_before = tuple(spotify.replacements)
    calls_before = spotify.show_calls

    rejected = runner.run_cycle()

    assert not rejected.ok
    assert spotify.show_calls == calls_before + 2
    assert tuple(spotify.replacements) == writes_before
    durable = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert durable is not None
    assert durable.status is MatchStatus.MATCHED
    assert durable.spotify_episode_uri == SER_URI
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert recovery is not None
    assert recovery.last_attempt_at == NOW
    assert recovery.next_retry_at == NOW + timedelta(hours=6)
    calls_after_rejection = spotify.show_calls

    for minutes in (10, 20, 30):
        clock[0] = NOW + timedelta(minutes=minutes)
        blocked = runner.run_cycle()
        assert not blocked.ok
        assert spotify.show_calls == calls_after_rejection
        assert tuple(spotify.replacements) == writes_before

    clock[0] = NOW + timedelta(minutes=40)
    restarted = EngineRunner(
        _config("first"),
        type(store)(store.path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert not restarted.run_cycle().ok
    assert spotify.show_calls == calls_after_rejection
    assert tuple(spotify.replacements) == writes_before

    clock[0] = NOW + timedelta(hours=6)
    recovered = restarted.run_cycle()
    assert recovered.ok
    assert spotify.show_calls == calls_after_rejection + 2
    durable = restarted.store.get_match_state(SourceId("ser"), "ser-1")
    assert durable is not None
    assert durable.status is MatchStatus.MATCHED
    assert durable.spotify_episode_uri == REPLACEMENT_URI
    assert restarted.planner_journal.get_match_input("ser", "ser-1") is not None
    assert restarted.planner_journal.get_matched_recovery("ser", "ser-1") is None
    assert spotify.items["destination-first"] == [REPLACEMENT_URI]
    assert spotify.replacements[-1] == ("destination-first", (REPLACEMENT_URI,))


def test_complete_absence_rejected_repair_never_enables_soft_retention(tmp_path: Path) -> None:
    clock = [NOW]
    spotify = _RollbackReplacementSpotify()
    store = _store(tmp_path)
    runner = EngineRunner(
        _config("first"),
        store,
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    runner.planner_journal.clear_match_input("ser", "ser-1")
    spotify.replacement_catalogue = True
    clock[0] = NOW - timedelta(minutes=10)

    rejected = runner.run_cycle()

    assert not rejected.ok
    durable = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert durable is not None and durable.spotify_episode_uri == SER_URI
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None

    playlist = _playlist("first")
    logical = build_desired_state_from_store(
        runner.store,
        playlist,
        now=NOW + timedelta(hours=49),
        source_timezones={SourceId("ser"): _source().timezone},
    )
    evidence = resolve_soft_retention_evidence(
        runner.store,
        {SourceId("ser"): _source()},
        playlist,
        logical,
        now=NOW + timedelta(hours=49),
        auth_context_fingerprint="a" * 64,
    )
    assert evidence is None


def test_fresh_resolution_rejected_by_temporal_guard_never_qualifies(tmp_path: Path) -> None:
    source = _matcher_source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        source,
        "ser-future-pending",
        "Las noticias de la SER, 10:00 (14/09/2026)",
        duration_seconds=60,
    )
    store = _store(tmp_path)
    store.upsert_editions((edition,), observed_at=NOW)
    assert store.set_match_state(
        *edition.identity,
        status=MatchStatus.PENDING,
        diagnostics="future durable pending",
        updated_at=NOW + timedelta(hours=4),
    )
    page = {
        "items": [_candidate(edition, source, uri=REPLACEMENT_URI, duration_ms=60_000)],
        "next": None,
    }

    result = match_source_editions(
        FakeCatalogClient(pages=[page]),
        store,
        source,
        (edition,),
        now=NOW,
        evaluation_intents={edition.identity: MatchEvaluationIntent.SEMANTIC_REEVALUATION},
    )
    outcome = result.outcomes[0]

    assert result.persistence_rejected
    assert outcome.persistence_rejected
    assert outcome.from_cache
    assert outcome.status is MatchStatus.PENDING
    assert outcome.spotify_episode_uri is None
    assert outcome.planner_input_evidence_qualified is None
    durable = store.get_match_state(*edition.identity)
    assert durable is not None
    assert durable.status is MatchStatus.PENDING
    assert durable.updated_at == NOW + timedelta(hours=4)


def test_shared_catalogue_preserves_rejected_repair_barrier(tmp_path: Path) -> None:
    spotify = _RollbackReplacementSpotify()
    runner, spotify, _, _ = setup(tmp_path, spotify=spotify)

    runner.clock = lambda: NOW + timedelta(hours=4)
    assert runner.run_cycle().ok

    runner.clock = lambda: NOW
    runner.planner_journal.clear_match_input("ser", "ser-1")
    runner.planner_journal.mark_destination_unknown("first")
    spotify.items["destination-first"] = []
    spotify.replacement_catalogue = True
    spotify.include_invalid_cached = True

    rejected = runner.run_cycle()
    assert not rejected.ok
    recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert recovery is not None
    assert recovery.next_retry_at == NOW + timedelta(hours=6)
    writes_before = tuple(spotify.replacements)
    calls_after_rejection = spotify.show_calls

    # Another native asset legitimately wakes the shared show catalogue while A remains inside
    # its persistence-rejection backoff. Shadow qualification may clear stale positive evidence,
    # but it must not clear or rebase the rejection barrier.
    runner.clock = lambda: NOW + timedelta(minutes=10)
    runner.fetcher = lambda _url: _rss_two()
    shared = runner.run_cycle()

    durable = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert durable is not None
    assert durable.status is MatchStatus.MATCHED
    assert durable.spotify_episode_uri == SER_URI
    assert durable.updated_at == NOW + timedelta(hours=4)
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    assert runner.planner_journal.get_matched_recovery("ser", "ser-1") == recovery
    assert tuple(spotify.replacements) == writes_before
    assert spotify.show_calls == calls_after_rejection + 2
    assert not shared.ok

    # The shared shadow inspection must not turn the next quiet cycle into an early recovery.
    runner.clock = lambda: NOW + timedelta(minutes=20)
    quiet = runner.run_cycle()
    assert not quiet.ok
    assert spotify.show_calls == calls_after_rejection + 2
    assert tuple(spotify.replacements) == writes_before
    assert runner.planner_journal.get_matched_recovery("ser", "ser-1") == recovery
