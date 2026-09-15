from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


matcher_path = Path("src/news_bulletin_playlist/spotify/matcher.py")
matcher = matcher_path.read_text()
matcher = replace_once(
    matcher,
    "from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore\n",
    "from news_bulletin_playlist.persistence import MatchStatus, PersistenceError, SQLiteStore\n",
    label="matcher persistence import",
)
matcher = replace_once(
    matcher,
    "    planner_input_evidence_qualified: bool | None = None\n\n\n@dataclass(frozen=True, slots=True)\nclass SourceMatchResult:\n",
    "    planner_input_evidence_qualified: bool | None = None\n"
    "    # True means a semantically-authoritative transition was attempted but SQLite's\n"
    "    # monotonic updated_at guard rejected it. The returned mapping remains aligned with\n"
    "    # durable state and Engine must keep planner evidence and destination writes fail-closed.\n"
    "    persistence_rejected: bool = False\n\n\n@dataclass(frozen=True, slots=True)\nclass SourceMatchResult:\n",
    label="MatchOutcome persistence flag",
)
matcher = replace_once(
    matcher,
    "    catalogue_inspected: bool = False\n\n\n@dataclass(frozen=True, slots=True)\nclass SpotifyCatalogueResult:\n",
    "    catalogue_inspected: bool = False\n\n"
    "    @property\n"
    "    def persistence_rejected(self) -> bool:\n"
    "        return any(outcome.persistence_rejected for outcome in self.outcomes)\n\n\n"
    "@dataclass(frozen=True, slots=True)\nclass SpotifyCatalogueResult:\n",
    label="SourceMatchResult persistence property",
)
old_complete = '''            store.set_match_state(
                cached.edition.source_id,
                cached.edition.source_native_id,
                status=MatchStatus.PENDING,
                spotify_episode_uri=None,
                spotify_duration_seconds=None,
                diagnostics=diagnostics,
                updated_at=observed_at,
            )
            outcomes[index] = MatchOutcome(
                edition=cached.edition,
                status=MatchStatus.PENDING,
                spotify_episode_uri=None,
                spotify_duration_seconds=None,
                diagnostics=diagnostics,
            )
            unresolved.append((index, cached.edition))
'''
new_complete = '''            applied = store.set_match_state(
                cached.edition.source_id,
                cached.edition.source_native_id,
                status=MatchStatus.PENDING,
                spotify_episode_uri=None,
                spotify_duration_seconds=None,
                diagnostics=diagnostics,
                updated_at=observed_at,
            )
            if not applied:
                # A complete catalogue positively contradicted the durable mapping, but a newer
                # durable row won the temporal ordering guard. Keep the public result aligned with
                # SQLite and make the rejection explicit so Engine can block writes and back off.
                outcomes[index] = replace(cached, persistence_rejected=True)
                qualification_overrides[index] = False
                continue
            outcomes[index] = MatchOutcome(
                edition=cached.edition,
                status=MatchStatus.PENDING,
                spotify_episode_uri=None,
                spotify_duration_seconds=None,
                diagnostics=diagnostics,
            )
            unresolved.append((index, cached.edition))
'''
matcher = replace_once(matcher, old_complete, new_complete, label="complete absence acknowledgement")
old_fresh = '''        store.set_match_state(
            edition.source_id,
            edition.source_native_id,
            status=outcome.status,
            spotify_episode_uri=outcome.spotify_episode_uri,
            spotify_duration_seconds=outcome.spotify_duration_seconds,
            diagnostics=outcome.diagnostics,
            updated_at=observed_at,
        )
        outcomes[index] = outcome
'''
new_fresh = '''        applied = store.set_match_state(
            edition.source_id,
            edition.source_native_id,
            status=outcome.status,
            spotify_episode_uri=outcome.spotify_episode_uri,
            spotify_duration_seconds=outcome.spotify_duration_seconds,
            diagnostics=outcome.diagnostics,
            updated_at=observed_at,
        )
        if not applied:
            durable = _persisted_outcome(store, edition, now=observed_at)
            if durable is None:
                raise PersistenceError(
                    "Spotify match write was rejected by temporal ordering and durable state "
                    "cannot be safely reused"
                )
            outcomes[index] = replace(durable, persistence_rejected=True)
            qualification_overrides[index] = None
            continue
        outcomes[index] = outcome
'''
matcher = replace_once(matcher, old_fresh, new_fresh, label="fresh resolution acknowledgement")
matcher = replace_once(
    matcher,
    '''            if not applied:
                # The durable row is newer than this runner observation (for example after a
                # clock rollback). Never publish a fresh repaired outcome that SQLite rejected:
                # planner evidence must remain fail-closed against the mapping still on disk.
                qualification_overrides[index] = assessment.qualification
                continue
''',
    '''            if not applied:
                # The durable row is newer than this runner observation (for example after a
                # clock rollback). Never publish a fresh repaired outcome that SQLite rejected:
                # planner evidence must remain fail-closed against the mapping still on disk.
                qualification_overrides[index] = assessment.qualification
                outcomes[index] = replace(outcome, persistence_rejected=True)
                continue
''',
    label="cached repair rejection flag",
)
matcher = replace_once(
    matcher,
    '''            store.set_match_state(
                edition.source_id,
                edition.source_native_id,
                status=MatchStatus.PENDING,
                spotify_episode_uri=None,
                spotify_duration_seconds=None,
                diagnostics=diagnostics,
                updated_at=now,
            )
            return None
''',
    '''            applied = store.set_match_state(
                edition.source_id,
                edition.source_native_id,
                status=MatchStatus.PENDING,
                spotify_episode_uri=None,
                spotify_duration_seconds=None,
                diagnostics=diagnostics,
                updated_at=now,
            )
            if not applied:
                raise PersistenceError(
                    "invalid persisted Spotify mapping could not be durably invalidated"
                )
            return None
''',
    label="malformed URI acknowledgement",
)
matcher_path.write_text(matcher)

engine_path = Path("src/news_bulletin_playlist/engine.py")
engine = engine_path.read_text()
engine = replace_once(
    engine,
    "        unsafe_source_ids: set[SourceId] = set()\n        matching_evaluated_counts: dict[SourceId, int] = {}\n",
    "        unsafe_source_ids: set[SourceId] = set()\n"
    "        persistence_rejected_source_ids: set[SourceId] = set()\n"
    "        matching_evaluated_counts: dict[SourceId, int] = {}\n",
    label="cycle persistence rejection set",
)
engine = replace_once(
    engine,
    '''                    self._observe_matching_result(source, match_result, matching_now)
                else:
''',
    '''                    self._observe_matching_result(source, match_result, matching_now)
                    if match_result.persistence_rejected:
                        persistence_rejected_source_ids.add(source.id)
                        unsafe_source_ids.add(source.id)
                else:
''',
    label="same-cycle unsafe source propagation",
)
engine = replace_once(
    engine,
    '''            request_diagnostics: dict[str, Any] = {}
            unsafe_for_playlist = tuple(
                source_id
                for source_id in playlist.source_selection.explicit
                if source_id in unsafe_source_ids
            )
''',
    '''            request_diagnostics: dict[str, Any] = {}
            rejected_for_playlist = tuple(
                source_id
                for source_id in playlist.source_selection.explicit
                if source_id in persistence_rejected_source_ids
                or self._source_has_persistence_rejection_barrier(
                    source_id, playlist_started_at
                )
            )
            unsafe_for_playlist = tuple(
                source_id
                for source_id in playlist.source_selection.explicit
                if source_id in unsafe_source_ids or source_id in rejected_for_playlist
            )
''',
    label="playlist durable rejection barrier",
)
engine = replace_once(
    engine,
    '''                    reason = (
                        "destination preserved because no last-known-good state is available "
                        "for source(s): "
                        + ", ".join(str(source_id) for source_id in unsafe_for_playlist)
                    )
''',
    '''                    if rejected_for_playlist:
                        reason = (
                            "destination preserved because a Spotify matching repair was "
                            "rejected by durable persistence for source(s): "
                            + ", ".join(str(source_id) for source_id in rejected_for_playlist)
                        )
                    else:
                        reason = (
                            "destination preserved because no last-known-good state is available "
                            "for source(s): "
                            + ", ".join(str(source_id) for source_id in unsafe_for_playlist)
                        )
''',
    label="playlist rejection reason",
)
engine = replace_once(
    engine,
    '''                        if reason is PlannerReason.INPUT_CHANGED:
                            intents[edition.identity] = MatchEvaluationIntent.SEMANTIC_REEVALUATION
                            input_changed_count += 1
                        elif reason is not None:
                            intents[edition.identity] = MatchEvaluationIntent.EVIDENCE_RECOVERY
                            evidence_recovery_count += 1
                    else:
                        intents[edition.identity] = MatchEvaluationIntent.SEMANTIC_REEVALUATION
                        missing_evidence_count += 1
                    continue
''',
    '''                        if reason is PlannerReason.INPUT_CHANGED:
                            intents[edition.identity] = MatchEvaluationIntent.SEMANTIC_REEVALUATION
                            input_changed_count += 1
                        elif reason in {
                            PlannerReason.MISSING_EVIDENCE,
                            PlannerReason.RETRY_DUE,
                        }:
                            intents[edition.identity] = MatchEvaluationIntent.EVIDENCE_RECOVERY
                            evidence_recovery_count += 1
                    else:
                        retry = self.planner_journal.get_unresolved(
                            str(source.id), edition.source_native_id
                        )
                        reason = unresolved_planning_reason(
                            retry,
                            input_fingerprint=fingerprint,
                            now=now,
                        )
                        if reason is PlannerReason.MISSING_EVIDENCE:
                            intents[edition.identity] = MatchEvaluationIntent.SEMANTIC_REEVALUATION
                            missing_evidence_count += 1
                        elif reason is PlannerReason.INPUT_CHANGED:
                            intents[edition.identity] = MatchEvaluationIntent.SEMANTIC_REEVALUATION
                            input_changed_count += 1
                        elif reason is PlannerReason.RETRY_DUE:
                            intents[edition.identity] = MatchEvaluationIntent.SEMANTIC_REEVALUATION
                            retry_due_count += 1
                        elif reason is None:
                            coalescing_candidates.append(edition.identity)
                    continue
''',
    label="clock rollback and unresolved rejection scheduling",
)
anchor = '''    def _plan_destinations(
        self, now: datetime, unsafe_source_ids: set[SourceId]
    ) -> tuple[set[PlaylistId], set[PlaylistId]]:
'''
helpers = '''    def _edition_has_persistence_rejection_barrier(
        self,
        source: SourceDefinition,
        edition: CanonicalEdition,
    ) -> bool:
        """Detect durable evidence that a newer match rejected the last evaluation attempt."""
        match = self.store.get_match_state(source.id, edition.source_native_id)
        if match is None:
            return False
        fingerprint = matching_input_fingerprint(source, edition)
        if match.status is MatchStatus.MATCHED:
            recovery = self.planner_journal.get_matched_recovery(
                str(source.id), edition.source_native_id
            )
            return bool(
                recovery is not None
                and recovery.input_fingerprint == fingerprint
                and match.updated_at > recovery.last_attempt_at
            )
        unresolved = self.planner_journal.get_unresolved(
            str(source.id), edition.source_native_id
        )
        return bool(
            unresolved is not None
            and unresolved.input_fingerprint == fingerprint
            and match.updated_at > unresolved.last_attempt_at
        )

    def _source_has_persistence_rejection_barrier(
        self,
        source_id: SourceId,
        now: datetime,
    ) -> bool:
        source = next(
            (candidate for candidate in required_sources(self.config) if candidate.id == source_id),
            None,
        )
        if source is None:
            return False
        matching_cutoff = self._matching_cutoff(now)
        return any(
            matching_cutoff <= edition.published_at <= now
            and self._edition_has_persistence_rejection_barrier(source, edition)
            for edition in self.store.list_editions(source_id=source_id)
        )

'''
engine = replace_once(engine, anchor, helpers + anchor, label="durable rejection barrier helpers")
engine = replace_once(
    engine,
    '''                current = self.store.get_match_state(item.source_id, item.source_native_id)
''',
    '''                edition = self.store.get_edition(item.source_id, item.source_native_id)
                if edition is None:
                    raise SpotifyReconciliationError(
                        "Spotify prewrite validation lost its canonical edition; "
                        "destination preserved"
                    )
                if self._edition_has_persistence_rejection_barrier(source, edition):
                    raise SpotifyReconciliationError(
                        "Spotify prewrite validation found a temporally rejected matching repair; "
                        "destination preserved"
                    )
                current = self.store.get_match_state(item.source_id, item.source_native_id)
''',
    label="prewrite barrier before presence shortcut",
)
engine = replace_once(
    engine,
    '''                edition = self.store.get_edition(item.source_id, item.source_native_id)
                if edition is None:
                    raise SpotifyReconciliationError(
                        "Spotify prewrite validation lost its canonical edition; "
                        "destination preserved"
                    )
                result = match_source_editions(
''',
    '''                result = match_source_editions(
''',
    label="remove duplicate prewrite edition lookup",
)
engine = replace_once(
    engine,
    '''                self._observe_matching_result(source, result, observed_at)

            return build_desired_state_from_store(
''',
    '''                self._observe_matching_result(source, result, observed_at)
                if result.persistence_rejected:
                    raise SpotifyReconciliationError(
                        "Spotify prewrite validation could not durably apply matching evidence; "
                        "destination preserved"
                    )

            return build_desired_state_from_store(
''',
    label="prewrite same-cycle rejection",
)
engine = replace_once(
    engine,
    '''            for outcome in result.outcomes:
                edition = outcome.edition
                input_fingerprint = matching_input_fingerprint(source, edition)
                if outcome.planner_input_evidence_qualified is True:
''',
    '''            for outcome in result.outcomes:
                edition = outcome.edition
                input_fingerprint = matching_input_fingerprint(source, edition)
                if outcome.persistence_rejected:
                    # A newer durable match rejected this wall-clock observation. Evidence for the
                    # attempted state is non-authoritative, the destination must remain closed, and
                    # retries must use a bounded durable schedule instead of CLOCK_ROLLBACK polling.
                    self.planner_journal.clear_match_input(
                        str(edition.source_id), edition.source_native_id
                    )
                    self.planner_journal.clear_matched_recovery(
                        str(edition.source_id), edition.source_native_id
                    )
                    self.planner_journal.clear_unresolved(
                        str(edition.source_id), edition.source_native_id
                    )
                    if outcome.status is MatchStatus.MATCHED:
                        self.planner_journal.record_matched_recovery(
                            source_id=str(edition.source_id),
                            source_native_id=edition.source_native_id,
                            input_fingerprint=input_fingerprint,
                            attempted_at=observed_at,
                        )
                    else:
                        self.planner_journal.record_unresolved(
                            source_id=str(edition.source_id),
                            source_native_id=edition.source_native_id,
                            input_fingerprint=input_fingerprint,
                            status=outcome.status,
                            attempted_at=observed_at,
                        )
                    continue
                if outcome.planner_input_evidence_qualified is True:
''',
    label="observer rejected persistence scheduling",
)
engine_path.write_text(engine)

# Focused integration regressions for the two blockers found by the third adversarial review.
test_path = Path("tests/test_pr211_persistence_rejection_barriers.py")
test_path.write_text('''from __future__ import annotations

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
    _Spotify,
    _config,
    _playlist,
    _rss,
    _source,
    _store,
)
from test_spotify_matcher import FakeCatalogClient, _candidate, _edition, _source as _matcher_source

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
''')
