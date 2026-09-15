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


def test_shared_complete_absence_does_not_rebase_rejected_repair_deadline(
    tmp_path: Path,
) -> None:
    spotify = _RollbackReplacementSpotify()
    runner, spotify, auth, _ = setup(tmp_path, spotify=spotify)

    runner.clock = lambda: NOW + timedelta(hours=4)
    assert runner.run_cycle().ok

    runner.clock = lambda: NOW
    runner.planner_journal.clear_match_input("ser", "ser-1")
    runner.planner_journal.mark_destination_unknown("first")
    spotify.items["destination-first"] = []
    spotify.replacement_catalogue = True
    spotify.include_invalid_cached = True
    assert not runner.run_cycle().ok
    recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert recovery is not None
    assert recovery.next_retry_at == NOW + timedelta(hours=6)
    writes_before = tuple(spotify.replacements)
    calls_after_rejection = spotify.show_calls

    # A different native asset wakes the shared catalogue shortly before A's deadline. A is now
    # absent from the complete catalogue, so its temporal invalidation is rejected again. That
    # incidental rejection must preserve the original deadline rather than postponing recovery.
    spotify.include_invalid_cached = False
    original_show_episodes = spotify.show_episodes

    def show_episodes(show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        page = original_show_episodes(show_id, limit=limit, offset=offset)
        if offset == 0:
            page["items"][1]["name"] = "Las noticias de la SER, 10:00 (30/08/2026)"
        return page

    spotify.show_episodes = show_episodes  # type: ignore[method-assign]
    runner.fetcher = lambda _url: _rss_two()
    runner.clock = lambda: NOW + timedelta(hours=3, minutes=50)
    shared = runner.run_cycle()
    assert not shared.ok
    assert shared.sources[0].matching_diagnostics.matching_reason_new_match_count == 1
    assert shared.sources[0].matching_diagnostics.matching_reason_evidence_recovery_count == 0
    assert spotify.show_calls == calls_after_rejection + 2
    assert tuple(spotify.replacements) == writes_before
    assert runner.planner_journal.get_matched_recovery("ser", "ser-1") == recovery

    restarted = EngineRunner(
        _config("first"),
        type(runner.store)(runner.store.path),
        auth,
        fetcher=lambda _url: _rss_two(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW + timedelta(hours=6),
    )
    recovered = restarted.run_cycle()
    assert recovered.ok
    assert spotify.show_calls == calls_after_rejection + 4
    assert restarted.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == REPLACEMENT_URI
    assert restarted.planner_journal.get_matched_recovery("ser", "ser-1") is None
    assert spotify.replacements[-1][1][0] == REPLACEMENT_URI


def test_exact_shared_shadow_does_not_resolve_rejected_repair_barrier(tmp_path: Path) -> None:
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
    assert not runner.run_cycle().ok
    recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert recovery is not None
    writes_before = tuple(spotify.replacements)

    # A later shared catalogue exactly supports durable A. It may inform diagnostics, but only
    # A's explicit retry may resolve the barrier created by its rejected repair.
    original_show_episodes = spotify.show_episodes

    def show_episodes(show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        page = original_show_episodes(show_id, limit=limit, offset=offset)
        for item in page["items"]:
            if item["uri"] == SER_URI:
                item["name"] = SER_TITLE
            elif item["uri"] == REPLACEMENT_URI:
                item["name"] = "unrelated"
        if offset == 0:
            page["items"][1]["name"] = "Las noticias de la SER, 10:00 (30/08/2026)"
        return page

    spotify.show_episodes = show_episodes  # type: ignore[method-assign]
    runner.fetcher = lambda _url: _rss_two()
    runner.clock = lambda: NOW + timedelta(minutes=10)
    shared = runner.run_cycle()

    assert not shared.ok
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    assert runner.planner_journal.get_matched_recovery("ser", "ser-1") == recovery
    assert tuple(spotify.replacements) == writes_before


def test_inconclusive_due_retry_keeps_rejected_barrier_across_restart(
    tmp_path: Path,
) -> None:
    spotify = _RollbackReplacementSpotify()
    runner, spotify, auth, _ = setup(
        tmp_path,
        spotify=spotify,
        playlists=("first", "second"),
    )

    # A durable mapping from 14:00 is contradicted after the runner clock rolls back to 10:00.
    runner.clock = lambda: NOW + timedelta(hours=4)
    assert runner.run_cycle().ok
    runner.clock = lambda: NOW
    runner.planner_journal.clear_match_input("ser", "ser-1")
    for playlist_id in ("first", "second"):
        runner.planner_journal.mark_destination_unknown(playlist_id)
        spotify.items[f"destination-{playlist_id}"] = []
    spotify.replacement_catalogue = True
    spotify.include_invalid_cached = True

    rejected = runner.run_cycle()
    assert not rejected.ok
    initial_recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert initial_recovery is not None
    assert initial_recovery.first_attempt_at == NOW
    assert initial_recovery.last_attempt_at == NOW
    assert initial_recovery.next_retry_at == NOW + timedelta(hours=6)
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    writes_before = tuple(spotify.replacements)
    calls_after_rejection = spotify.show_calls

    # At the explicit retry deadline the bounded catalogue is truncated and does not contain A.
    # This is inconclusive: scheduling advances, but the earlier rejected repair is unresolved.
    replacement_show_episodes = spotify.show_episodes

    def truncated_show_episodes(
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "ser-show"
        assert limit == 50
        assert offset in {0, 50}
        spotify.show_calls += 1
        items = [
            {
                "uri": f"spotify:episode:{offset + index + 3000:022d}",
                "name": "unrelated",
                "release_date": "2026-08-30",
                "release_date_precision": "day",
                "duration_ms": 60_000,
            }
            for index in range(50)
        ]
        return catalogue_page(
            items,
            offset=offset,
            limit=limit,
            next_url="more",
            total=101,
        )

    spotify.show_episodes = truncated_show_episodes  # type: ignore[method-assign]
    runner.clock = lambda: initial_recovery.next_retry_at
    due = runner.run_cycle()
    assert not due.ok
    assert spotify.show_calls == calls_after_rejection + 2
    assert tuple(spotify.replacements) == writes_before
    due_recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert due_recovery is not None
    assert due_recovery.first_attempt_at == initial_recovery.first_attempt_at
    assert due_recovery.last_attempt_at == NOW + timedelta(hours=6)
    assert due_recovery.next_retry_at == NOW + timedelta(hours=12)
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    durable = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert durable is not None
    assert durable.status is MatchStatus.MATCHED
    assert durable.spotify_episode_uri == SER_URI
    assert durable.updated_at == NOW + timedelta(hours=4)

    # Restart, then let a different native asset legitimately pay for the same show catalogue.
    # A is only shadow-observed here. Its old contradiction is still unresolved, so neither
    # destination may be reconciled with A merely because A is physically present in the show.
    spotify.show_episodes = replacement_show_episodes  # type: ignore[method-assign]
    calls_before_shared = spotify.show_calls
    restarted = EngineRunner(
        _config("first", "second"),
        type(runner.store)(runner.store.path),
        auth,
        fetcher=lambda _url: _rss_two(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW + timedelta(hours=6, minutes=10),
    )
    shared = restarted.run_cycle()

    assert not shared.ok
    diagnostics = shared.sources[0].matching_diagnostics
    assert diagnostics.matching_reason_new_match_count == 1
    assert diagnostics.matching_reason_evidence_recovery_count == 0
    assert spotify.show_calls == calls_before_shared + 2
    assert tuple(spotify.replacements) == writes_before
    assert restarted.planner_journal.get_match_input("ser", "ser-1") is None
    assert restarted.planner_journal.get_matched_recovery("ser", "ser-1") == due_recovery
    assert restarted.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    assert spotify.items["destination-first"] == []
    assert spotify.items["destination-second"] == []


def test_input_fingerprint_change_cannot_erase_rejected_repair_barrier(
    tmp_path: Path,
) -> None:
    spotify = _RollbackReplacementSpotify()
    auth = _Auth()

    def feed(*, corrected: bool, sibling: bool = False) -> bytes:
        published = (
            "Sun, 30 Aug 2026 09:06:00 +0000" if corrected else "Sun, 30 Aug 2026 09:05:00 +0000"
        )
        items = [
            f"<item><guid>ser-1</guid><title>{SER_TITLE}</title>"
            f"<pubDate>{published}</pubDate></item>"
        ]
        if sibling:
            items.append(
                "<item><guid>ser-2</guid>"
                "<title>Las noticias de la SER, 12:00 (30/08/2026)</title>"
                "<pubDate>Sun, 30 Aug 2026 10:05:00 +0000</pubDate></item>"
            )
        return f"<rss><channel>{''.join(items)}</channel></rss>".encode()

    runner = EngineRunner(
        _config("first", "second"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: feed(corrected=False),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW + timedelta(hours=4),
    )
    assert runner.run_cycle().ok

    runner.clock = lambda: NOW
    runner.planner_journal.clear_match_input("ser", "ser-1")
    for playlist_id in ("first", "second"):
        runner.planner_journal.mark_destination_unknown(playlist_id)
        spotify.items[f"destination-{playlist_id}"] = []
    spotify.replacement_catalogue = True
    spotify.include_invalid_cached = True
    writes_before = tuple(spotify.replacements)

    rejected = runner.run_cycle()
    assert not rejected.ok
    barrier = runner.planner_journal.get_persistence_rejection_barrier("ser", "ser-1")
    assert barrier is not None
    assert barrier.first_rejected_at == NOW
    initial_recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert initial_recovery is not None
    assert initial_recovery.next_retry_at == NOW + timedelta(hours=6)
    calls_after_rejection = spotify.show_calls

    # At 15:00 only pubDate changes. INPUT_CHANGED is legitimate, but the bounded catalogue is
    # truncated and cannot establish whether durable A is still valid. Scheduling may reset for
    # the new fingerprint; destination-safety memory must not.
    def truncated_show_episodes(
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "ser-show"
        assert limit == 50
        assert offset in {0, 50}
        spotify.show_calls += 1
        items = [
            {
                "uri": f"spotify:episode:{offset + index + 5000:022d}",
                "name": "unrelated",
                "release_date": "2026-08-30",
                "release_date_precision": "day",
                "duration_ms": 60_000,
            }
            for index in range(50)
        ]
        return catalogue_page(
            items,
            offset=offset,
            limit=limit,
            next_url="more",
            total=101,
        )

    replacement_show_episodes = spotify.show_episodes
    spotify.show_episodes = truncated_show_episodes  # type: ignore[method-assign]
    runner.fetcher = lambda _url: feed(corrected=True)
    runner.clock = lambda: NOW + timedelta(hours=5)
    changed = runner.run_cycle()

    assert not changed.ok
    diagnostics = changed.sources[0].matching_diagnostics
    assert diagnostics.matching_reason_input_changed_count == 1
    assert spotify.show_calls == calls_after_rejection + 2
    changed_recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert changed_recovery is not None
    assert changed_recovery.first_attempt_at == NOW + timedelta(hours=5)
    assert changed_recovery.next_retry_at == NOW + timedelta(hours=11)
    assert runner.planner_journal.get_persistence_rejection_barrier("ser", "ser-1") == barrier
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    assert tuple(spotify.replacements) == writes_before

    # Restart at 15:10. A sibling NEW_MATCH pays for the shared show catalogue; A is shadow-only.
    # The catalogue again contradicts A and identifies B, but without explicit authority for A it
    # cannot erase the durable barrier or authorize PUT(A) into either empty destination.
    spotify.show_episodes = replacement_show_episodes  # type: ignore[method-assign]
    calls_before_shared = spotify.show_calls
    restarted = EngineRunner(
        _config("first", "second"),
        type(runner.store)(runner.store.path),
        auth,
        fetcher=lambda _url: feed(corrected=True, sibling=True),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW + timedelta(hours=5, minutes=10),
    )
    shared = restarted.run_cycle()

    assert not shared.ok
    diagnostics = shared.sources[0].matching_diagnostics
    assert diagnostics.matching_reason_new_match_count == 1
    assert diagnostics.matching_reason_input_changed_count == 0
    assert diagnostics.matching_reason_evidence_recovery_count == 0
    assert spotify.show_calls == calls_before_shared + 2
    assert tuple(spotify.replacements) == writes_before
    assert restarted.planner_journal.get_persistence_rejection_barrier("ser", "ser-1") == barrier
    assert restarted.planner_journal.get_matched_recovery("ser", "ser-1") == changed_recovery
    assert restarted.planner_journal.get_match_input("ser", "ser-1") is None
    assert restarted.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    assert spotify.items["destination-first"] == []
    assert spotify.items["destination-second"] == []

    # At the new fingerprint's due deadline, a conclusive explicit repair may finally resolve the
    # barrier, persist B, restore current evidence, and reconcile both destinations.
    restarted.fetcher = lambda _url: feed(corrected=True, sibling=False)
    restarted.clock = lambda: changed_recovery.next_retry_at
    resolved = restarted.run_cycle()

    assert resolved.ok
    assert restarted.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == REPLACEMENT_URI
    assert restarted.planner_journal.get_persistence_rejection_barrier("ser", "ser-1") is None
    assert restarted.planner_journal.get_matched_recovery("ser", "ser-1") is None
    assert restarted.planner_journal.get_match_input("ser", "ser-1") is not None
    assert spotify.items["destination-first"][0] == REPLACEMENT_URI
    assert spotify.items["destination-second"][0] == REPLACEMENT_URI
