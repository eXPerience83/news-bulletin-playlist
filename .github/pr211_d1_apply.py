from pathlib import Path
from textwrap import dedent

ENGINE_PATH = Path("src/news_bulletin_playlist/engine.py")
TEST_PATH = Path("tests/test_pr211_persistence_rejection_barriers.py")

engine = ENGINE_PATH.read_text()
old = """            return bool(
                recovery is not None
                and recovery.input_fingerprint == fingerprint
                and match.updated_at > recovery.last_attempt_at
            )
"""
new = """            return bool(
                recovery is not None
                and recovery.input_fingerprint == fingerprint
                # The first attempt anchors a temporal persistence rejection. Later
                # inconclusive retries advance last_attempt_at for scheduling only; they
                # must not erase the destination-safety barrier before a durable repair.
                and match.updated_at > recovery.first_attempt_at
            )
"""
if engine.count(old) != 1:
    raise SystemExit("expected MATCHED rejection-barrier predicate not found exactly once")
ENGINE_PATH.write_text(engine.replace(old, new))

tests = TEST_PATH.read_text()
test_name = "test_inconclusive_due_retry_keeps_rejected_barrier_across_restart"
if test_name in tests:
    raise SystemExit("D1 regression already exists")

tests += dedent(
    r"""

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
"""
)
TEST_PATH.write_text(tests)
