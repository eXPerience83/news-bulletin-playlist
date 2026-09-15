from pathlib import Path


engine = Path("src/news_bulletin_playlist/engine.py")
text = engine.read_text()

old = "                    self._observe_matching_result(source, match_result, matching_now)\n"
new = """                    self._observe_matching_result(
                        source,
                        match_result,
                        matching_now,
                        evaluation_intents=plan.evaluation_intents,
                    )
"""
if text.count(old) != 1:
    raise RuntimeError("expected main matching observation call exactly once")
text = text.replace(old, new, 1)

old = """                self._observe_matching_result(source, result, observed_at)
                if result.persistence_rejected:
"""
new = """                self._observe_matching_result(
                    source,
                    result,
                    observed_at,
                    evaluation_intents={
                        edition.identity: MatchEvaluationIntent.SEMANTIC_REEVALUATION
                    },
                )
                if result.persistence_rejected:
"""
if text.count(old) != 1:
    raise RuntimeError("expected prewrite matching observation call exactly once")
text = text.replace(old, new, 1)

old = """    def _observe_matching_result(
        self,
        source: SourceDefinition,
        result: SourceMatchResult,
        observed_at: datetime,
    ) -> None:
"""
new = """    def _observe_matching_result(
        self,
        source: SourceDefinition,
        result: SourceMatchResult,
        observed_at: datetime,
        *,
        evaluation_intents: Mapping[tuple[SourceId, str], MatchEvaluationIntent] | None = None,
    ) -> None:
"""
if text.count(old) != 1:
    raise RuntimeError("expected observe matching signature exactly once")
text = text.replace(old, new, 1)

old = """            for outcome in result.outcomes:
                edition = outcome.edition
                input_fingerprint = matching_input_fingerprint(source, edition)
                if outcome.persistence_rejected:
"""
new = """            for outcome in result.outcomes:
                edition = outcome.edition
                input_fingerprint = matching_input_fingerprint(source, edition)
                explicit_evaluation = bool(
                    evaluation_intents is not None
                    and edition.identity in evaluation_intents
                )
                rejection_barrier = self._edition_has_persistence_rejection_barrier(
                    source, edition
                )
                if outcome.persistence_rejected:
"""
if text.count(old) != 1:
    raise RuntimeError("expected outcome observation prologue exactly once")
text = text.replace(old, new, 1)

old = """                elif outcome.planner_input_evidence_qualified is False:
                    self.planner_journal.clear_match_input(
                        str(edition.source_id), edition.source_native_id
                    )
                    self.planner_journal.clear_matched_recovery(
                        str(edition.source_id), edition.source_native_id
                    )
"""
new = """                elif outcome.planner_input_evidence_qualified is False:
                    self.planner_journal.clear_match_input(
                        str(edition.source_id), edition.source_native_id
                    )
                    # A shadow-only contradiction can invalidate positive match-input evidence,
                    # but it did not attempt the repair whose temporal rejection created the
                    # durable destination barrier. Preserve that barrier until an explicit
                    # evaluation resolves it.
                    if explicit_evaluation or not rejection_barrier:
                        self.planner_journal.clear_matched_recovery(
                            str(edition.source_id), edition.source_native_id
                        )
"""
if text.count(old) != 1:
    raise RuntimeError("expected negative qualification branch exactly once")
text = text.replace(old, new, 1)

old = """                        if prior_input is None:
                            self.planner_journal.record_matched_recovery(
                                source_id=str(edition.source_id),
                                source_native_id=edition.source_native_id,
                                input_fingerprint=input_fingerprint,
                                attempted_at=observed_at,
                            )
"""
new = """                        if prior_input is None and not (
                            rejection_barrier and not explicit_evaluation
                        ):
                            self.planner_journal.record_matched_recovery(
                                source_id=str(edition.source_id),
                                source_native_id=edition.source_native_id,
                                input_fingerprint=input_fingerprint,
                                attempted_at=observed_at,
                            )
"""
if text.count(old) != 1:
    raise RuntimeError("expected inconclusive recovery branch exactly once")
text = text.replace(old, new, 1)
engine.write_text(text)

tests = Path("tests/test_pr211_persistence_rejection_barriers.py")
text = tests.read_text()
old = """    _rss,
    _source,
"""
new = """    _rss,
    _rss_two,
    _source,
"""
if text.count(old) != 1:
    raise RuntimeError("expected test_engine import block exactly once")
text = text.replace(old, new, 1)

marker = "from test_spotify_matcher import _source as _matcher_source\n"
insertion = (
    "from test_spotify_matcher import _source as _matcher_source\n"
    "from test_shadow_planner_review import setup\n"
)
if text.count(marker) != 1:
    raise RuntimeError("expected matcher source import exactly once")
text = text.replace(marker, insertion, 1)

addition = '''


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
'''
if "def test_shared_catalogue_preserves_rejected_repair_barrier" in text:
    raise RuntimeError("shared catalogue barrier test already exists")
tests.write_text(text + addition)
