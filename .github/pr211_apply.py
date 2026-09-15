from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one replacement target, found {count}")
    file.write_text(text.replace(old, new, 1))


matcher = "src/news_bulletin_playlist/spotify/matcher.py"
engine = "src/news_bulletin_playlist/engine.py"
planner = "src/news_bulletin_playlist/spotify_planner.py"

replace_once(
    matcher,
    '''@dataclass(frozen=True, slots=True)\nclass _CandidateEvaluation:\n    candidate: _SpotifyEpisodeCandidate\n    matched: bool\n    reason: str\n    release_delay_days: int = 0\n\n\ndef match_source_editions(\n''',
    '''@dataclass(frozen=True, slots=True)\nclass _CandidateEvaluation:\n    candidate: _SpotifyEpisodeCandidate\n    matched: bool\n    reason: str\n    release_delay_days: int = 0\n\n\n@dataclass(frozen=True, slots=True)\nclass _CachedMappingAssessment:\n    evaluated: MatchOutcome\n    qualification: bool | None\n    recovery_repair_allowed: bool\n\n\ndef match_source_editions(\n''',
)

replace_once(
    matcher,
    '''    notified_fresh_indices: set[int] = set()\n    duration_tolerance = duration_disambiguation_tolerance_seconds(source)\n\n    for index, edition in enumerate(editions):\n''',
    '''    notified_fresh_indices: set[int] = set()\n    duration_tolerance = duration_disambiguation_tolerance_seconds(source)\n    qualification_overrides: dict[int, bool | None] = {}\n\n    for index, edition in enumerate(editions):\n''',
)

replace_once(
    matcher,
    '''    if evaluation_intents is not None:\n        for index, value in enumerate(outcomes):\n            outcome = _require_outcome(value)\n            intent = evaluation_intents.get(outcome.edition.identity)\n            if (\n                outcome.status is not MatchStatus.MATCHED\n                or intent\n                not in {\n                    MatchEvaluationIntent.SEMANTIC_REEVALUATION,\n                    MatchEvaluationIntent.EVIDENCE_RECOVERY,\n                }\n                or outcome.spotify_episode_uri not in candidate_uris\n            ):\n                continue\n            evaluations = tuple(\n                _evaluate_candidate(source, outcome.edition, item) for item in candidates\n            )\n            evaluated = _resolve_outcome(\n                outcome.edition,\n                evaluations,\n                candidate_count=len(candidates),\n                catalogue_calls=catalogue_calls,\n                duration_tolerance_seconds=duration_tolerance,\n            )\n            if (\n                intent is MatchEvaluationIntent.EVIDENCE_RECOVERY\n                and not _recovery_can_repair_cached(outcome, evaluated, evaluations)\n            ):\n                continue\n            _notify_fresh_evaluation(\n                on_fresh_evaluation,\n                evaluated,\n                index=index,\n                notified_indices=notified_fresh_indices,\n            )\n            store.set_match_state(\n                outcome.edition.source_id,\n                outcome.edition.source_native_id,\n                status=evaluated.status,\n                spotify_episode_uri=evaluated.spotify_episode_uri,\n                spotify_duration_seconds=evaluated.spotify_duration_seconds,\n                diagnostics=evaluated.diagnostics,\n                updated_at=observed_at,\n            )\n            outcomes[index] = evaluated\n\n''',
    '''    if evaluation_intents is not None:\n        for index, value in enumerate(outcomes):\n            outcome = _require_outcome(value)\n            intent = evaluation_intents.get(outcome.edition.identity)\n            if (\n                not outcome.from_cache\n                or outcome.status is not MatchStatus.MATCHED\n                or intent\n                not in {\n                    MatchEvaluationIntent.SEMANTIC_REEVALUATION,\n                    MatchEvaluationIntent.EVIDENCE_RECOVERY,\n                }\n                or outcome.spotify_episode_uri not in candidate_uris\n            ):\n                continue\n            evaluations = tuple(\n                _evaluate_candidate(source, outcome.edition, item) for item in candidates\n            )\n            assessment = _assess_cached_mapping(\n                outcome,\n                evaluations,\n                candidate_count=len(candidates),\n                catalogue_calls=catalogue_calls,\n                duration_tolerance_seconds=duration_tolerance,\n            )\n            _notify_fresh_evaluation(\n                on_fresh_evaluation,\n                assessment.evaluated,\n                index=index,\n                notified_indices=notified_fresh_indices,\n            )\n            if assessment.qualification is True:\n                qualification_overrides[index] = True\n                continue\n            if assessment.qualification is None and not (\n                intent is MatchEvaluationIntent.EVIDENCE_RECOVERY\n                and assessment.recovery_repair_allowed\n            ):\n                qualification_overrides[index] = None\n                continue\n\n            evaluated = assessment.evaluated\n            store.set_match_state(\n                outcome.edition.source_id,\n                outcome.edition.source_native_id,\n                status=evaluated.status,\n                spotify_episode_uri=evaluated.spotify_episode_uri,\n                spotify_duration_seconds=evaluated.spotify_duration_seconds,\n                diagnostics=evaluated.diagnostics,\n                updated_at=observed_at,\n            )\n            outcomes[index] = evaluated\n\n''',
)

replace_once(
    matcher,
    '''        qualified: bool | None = None\n        if not outcome.from_cache:\n            qualified = True\n        elif outcome.status is MatchStatus.MATCHED and candidates:\n''',
    '''        qualified: bool | None = None\n        if index in qualification_overrides:\n            qualified = qualification_overrides[index]\n        elif not outcome.from_cache:\n            qualified = True\n        elif outcome.status is MatchStatus.MATCHED and candidates:\n''',
)

old_helpers = '''def _recovery_can_repair_cached(\n    cached: MatchOutcome,\n    evaluated: MatchOutcome,\n    evaluations: Sequence[_CandidateEvaluation],\n) -> bool:\n    """Reuse paid recovery work only when it gives conclusive new production evidence."""\n    cached_uri = cached.spotify_episode_uri\n    if cached_uri is None:\n        return False\n    cached_evaluation = next(\n        (evaluation for evaluation in evaluations if evaluation.candidate.uri == cached_uri),\n        None,\n    )\n    if cached_evaluation is None:\n        # Bounded absence is handled by the existing complete/incomplete catalogue rules.\n        return False\n    if not cached_evaluation.matched:\n        # The persisted mapping itself is positively contradicted. Persist the best current\n        # resolved state, even when that state is PENDING/AMBIGUOUS, instead of polling again.\n        return True\n    if evaluated.status is not MatchStatus.MATCHED:\n        # The cached URI remains viable but no unique mapping can be certified.\n        return False\n    return (\n        evaluated.spotify_episode_uri != cached_uri\n        or evaluated.spotify_duration_seconds != cached.spotify_duration_seconds\n    )\n\n\ndef _qualify_cached_mapping(\n    outcome: MatchOutcome,\n    evaluations: Sequence[_CandidateEvaluation],\n    *,\n    candidate_count: int,\n    catalogue_calls: int,\n    duration_tolerance_seconds: int | None,\n) -> bool | None:\n    """Classify cached evidence without turning ambiguity into contradiction."""\n    cached_uri = outcome.spotify_episode_uri\n    if cached_uri is None:\n        return False\n    cached_evaluation = next(\n        (evaluation for evaluation in evaluations if evaluation.candidate.uri == cached_uri),\n        None,\n    )\n    if cached_evaluation is None:\n        return None\n    if not cached_evaluation.matched:\n        return False\n\n    evaluated = _resolve_outcome(\n        outcome.edition,\n        evaluations,\n        candidate_count=candidate_count,\n        catalogue_calls=catalogue_calls,\n        duration_tolerance_seconds=duration_tolerance_seconds,\n    )\n    if evaluated.status is not MatchStatus.MATCHED:\n        # The persisted URI still passes semantic evaluation, but current evidence cannot\n        # select it uniquely. This is inconclusive and should enter bounded recovery backoff.\n        return None\n    if evaluated.spotify_episode_uri != cached_uri:\n        # Current matching rules uniquely prefer another semantic candidate.\n        return False\n    if evaluated.spotify_duration_seconds != outcome.spotify_duration_seconds:\n        # The URI remains uniquely correct, but exact provider metadata changed. Do not certify\n        # stale duration evidence. An explicit evidence-recovery intent repairs it in-place.\n        return None\n    return True\n\n\n'''

new_helpers = '''def _assess_cached_mapping(\n    outcome: MatchOutcome,\n    evaluations: Sequence[_CandidateEvaluation],\n    *,\n    candidate_count: int,\n    catalogue_calls: int,\n    duration_tolerance_seconds: int | None,\n) -> _CachedMappingAssessment:\n    """Classify one cached mapping once for production repair and planner evidence."""\n    evaluated = _resolve_outcome(\n        outcome.edition,\n        evaluations,\n        candidate_count=candidate_count,\n        catalogue_calls=catalogue_calls,\n        duration_tolerance_seconds=duration_tolerance_seconds,\n    )\n    cached_uri = outcome.spotify_episode_uri\n    if cached_uri is None:\n        return _CachedMappingAssessment(evaluated, False, False)\n\n    cached_evaluation = next(\n        (evaluation for evaluation in evaluations if evaluation.candidate.uri == cached_uri),\n        None,\n    )\n    if cached_evaluation is None:\n        # A bounded catalogue may simply not contain the durable URI. Absence handling is\n        # authoritative only in the existing complete/non-empty catalogue path above.\n        return _CachedMappingAssessment(evaluated, None, False)\n    if not cached_evaluation.matched:\n        # Positive semantic contradiction of the durable mapping. The already-paid catalogue may\n        # replace it with the best fresh state, including PENDING/AMBIGUOUS when no replacement is\n        # uniquely established.\n        return _CachedMappingAssessment(evaluated, False, True)\n    if evaluated.status is not MatchStatus.MATCHED:\n        # The durable URI is still viable, but uniqueness has been lost. Preserve it.\n        return _CachedMappingAssessment(evaluated, None, False)\n    if evaluated.spotify_episode_uri != cached_uri:\n        # Current matching rules uniquely prefer another candidate.\n        return _CachedMappingAssessment(evaluated, False, True)\n    if evaluated.spotify_duration_seconds == outcome.spotify_duration_seconds:\n        return _CachedMappingAssessment(evaluated, True, False)\n    if evaluated.spotify_duration_seconds is None:\n        # Missing provider metadata is not a conclusive metadata update and must never erase a\n        # known durable duration.\n        return _CachedMappingAssessment(evaluated, None, False)\n    # Same uniquely-valid URI with a newly known duration. Only explicit evidence recovery may\n    # refresh the durable metadata in-place; ordinary semantic reevaluation remains inconclusive.\n    return _CachedMappingAssessment(evaluated, None, True)\n\n\ndef _qualify_cached_mapping(\n    outcome: MatchOutcome,\n    evaluations: Sequence[_CandidateEvaluation],\n    *,\n    candidate_count: int,\n    catalogue_calls: int,\n    duration_tolerance_seconds: int | None,\n) -> bool | None:\n    """Return planner qualification from the shared cached-mapping assessment."""\n    return _assess_cached_mapping(\n        outcome,\n        evaluations,\n        candidate_count=candidate_count,\n        catalogue_calls=catalogue_calls,\n        duration_tolerance_seconds=duration_tolerance_seconds,\n    ).qualification\n\n\n'''
replace_once(matcher, old_helpers, new_helpers)

replace_once(
    engine,
    '''                if outcome.status is MatchStatus.MATCHED:\n                    self.planner_journal.clear_unresolved(\n                        str(edition.source_id), edition.source_native_id\n                    )\n                    if (\n                        result.catalogue_inspected\n                        and outcome.planner_input_evidence_qualified is None\n                        and self.planner_journal.get_match_input(\n                            str(edition.source_id), edition.source_native_id\n                        )\n                        is None\n                    ):\n                        self.planner_journal.record_matched_recovery(\n                            source_id=str(edition.source_id),\n                            source_native_id=edition.source_native_id,\n                            input_fingerprint=input_fingerprint,\n                            attempted_at=observed_at,\n                        )\n''',
    '''                if outcome.status is MatchStatus.MATCHED:\n                    self.planner_journal.clear_unresolved(\n                        str(edition.source_id), edition.source_native_id\n                    )\n                    if (\n                        result.catalogue_inspected\n                        and outcome.planner_input_evidence_qualified is None\n                    ):\n                        prior_input = self.planner_journal.get_match_input(\n                            str(edition.source_id), edition.source_native_id\n                        )\n                        if (\n                            prior_input is not None\n                            and prior_input.input_fingerprint != input_fingerprint\n                        ):\n                            self.planner_journal.clear_match_input(\n                                str(edition.source_id), edition.source_native_id\n                            )\n                            prior_input = None\n                        if prior_input is None:\n                            self.planner_journal.record_matched_recovery(\n                                source_id=str(edition.source_id),\n                                source_native_id=edition.source_native_id,\n                                input_fingerprint=input_fingerprint,\n                                attempted_at=observed_at,\n                            )\n''',
)

replace_once(
    planner,
    '''def source_contract_fingerprint(source: SourceDefinition) -> str:\n    references = sorted(\n        (ref.system.casefold(), ref.resource_type.casefold(), ref.external_id)\n        for ref in source.external_references\n    )\n    return _digest(\n        {\n            "v": _FINGERPRINT_VERSION,\n            "source_id": str(source.id),\n            "parser_id": str(source.parser_id),\n            "timezone": str(source.timezone),\n            "spotify_release_delay_days": source.spotify_release_delay_days,\n            "spotify_duration_disambiguation_tolerance_seconds": (\n                duration_disambiguation_tolerance_seconds(source)\n            ),\n            "external_references": references,\n        }\n    )\n''',
    '''def source_contract_fingerprint(source: SourceDefinition) -> str:\n    references = sorted(\n        (ref.system.casefold(), ref.resource_type.casefold(), ref.external_id)\n        for ref in source.external_references\n    )\n    contract: dict[str, object] = {\n        "v": _FINGERPRINT_VERSION,\n        "source_id": str(source.id),\n        "parser_id": str(source.parser_id),\n        "timezone": str(source.timezone),\n        "spotify_release_delay_days": source.spotify_release_delay_days,\n        "external_references": references,\n    }\n    duration_tolerance = duration_disambiguation_tolerance_seconds(source)\n    if duration_tolerance is not None:\n        # Preserve the exact legacy fingerprint payload for sources that did not opt in. This\n        # avoids an unrelated fleet-wide evidence invalidation merely because a new optional\n        # matching policy exists.\n        contract["spotify_duration_disambiguation_tolerance_seconds"] = duration_tolerance\n    return _digest(contract)\n''',
)

regression_test = r'''from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import news_bulletin_playlist.spotify_planner as spotify_planner
from test_ondacero_duration_disambiguation import ONDA_SHOW_ID, ONDA_TITLE, URI_251, URI_309
from test_spotify_matcher import FakeCatalogClient, _candidate, _edition, _source, _store

from news_bulletin_playlist.persistence import MatchStatus
from news_bulletin_playlist.spotify.matcher import MatchEvaluationIntent, match_source_editions
from news_bulletin_playlist.spotify.matching_policy import (
    duration_disambiguation_tolerance_seconds,
)

NOW = datetime(2026, 9, 15, 9, 30, tzinfo=UTC)


def _legacy_source_contract_fingerprint(source) -> str:
    references = sorted(
        (ref.system.casefold(), ref.resource_type.casefold(), ref.external_id)
        for ref in source.external_references
    )
    payload = {
        "v": 1,
        "source_id": str(source.id),
        "parser_id": str(source.parser_id),
        "timezone": str(source.timezone),
        "spotify_release_delay_days": source.spotify_release_delay_days,
        "external_references": references,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _persist_cached_match(store, edition, *, uri: str, duration: int) -> None:
    store.upsert_editions((edition,), observed_at=NOW)
    store.set_match_state(
        *edition.identity,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=uri,
        spotify_duration_seconds=duration,
        diagnostics="known mapping",
        updated_at=NOW - timedelta(hours=1),
    )


def test_semantic_reevaluation_preserves_viable_cached_mapping_when_uniqueness_is_lost(
    tmp_path,
) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        source,
        "ser-cached",
        "Las noticias de la SER, 10:00 (14/09/2026)",
        duration_seconds=60,
    )
    store = _store(tmp_path)
    _persist_cached_match(store, edition, uri=URI_309, duration=60)
    page = {
        "items": [
            _candidate(edition, source, uri=URI_309, duration_ms=60_000),
            _candidate(edition, source, uri=URI_251, duration_ms=61_000),
        ],
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

    assert outcome.from_cache
    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == URI_309
    assert outcome.spotify_duration_seconds == 60
    assert outcome.planner_input_evidence_qualified is None
    persisted = store.get_match_state(*edition.identity)
    assert persisted is not None
    assert persisted.status is MatchStatus.MATCHED
    assert persisted.spotify_episode_uri == URI_309
    assert persisted.spotify_duration_seconds == 60


def test_evidence_recovery_never_replaces_known_duration_with_unknown(tmp_path) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        source,
        "ser-duration-unknown",
        "Las noticias de la SER, 10:00 (14/09/2026)",
        duration_seconds=60,
    )
    store = _store(tmp_path)
    _persist_cached_match(store, edition, uri=URI_309, duration=60)
    item = _candidate(edition, source, uri=URI_309, duration_ms=60_000)
    item["duration_ms"] = None

    result = match_source_editions(
        FakeCatalogClient(pages=[{"items": [item], "next": None}]),
        store,
        source,
        (edition,),
        now=NOW,
        evaluation_intents={edition.identity: MatchEvaluationIntent.EVIDENCE_RECOVERY},
    )
    outcome = result.outcomes[0]

    assert outcome.from_cache
    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == URI_309
    assert outcome.spotify_duration_seconds == 60
    assert outcome.planner_input_evidence_qualified is None
    persisted = store.get_match_state(*edition.identity)
    assert persisted is not None
    assert persisted.spotify_duration_seconds == 60


def test_non_opted_source_keeps_exact_legacy_contract_fingerprint() -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    assert duration_disambiguation_tolerance_seconds(source) is None
    assert spotify_planner.source_contract_fingerprint(source) == _legacy_source_contract_fingerprint(
        source
    )


def test_onda_policy_changes_contract_and_tolerance_changes_invalidate_it(monkeypatch) -> None:
    source = _source("ondacero", "ondacero", "Europe/Madrid", ONDA_SHOW_ID)
    assert duration_disambiguation_tolerance_seconds(source) == 1
    actual = spotify_planner.source_contract_fingerprint(source)
    assert actual != _legacy_source_contract_fingerprint(source)

    monkeypatch.setattr(
        spotify_planner,
        "duration_disambiguation_tolerance_seconds",
        lambda _source: 2,
    )
    assert spotify_planner.source_contract_fingerprint(source) != actual


def test_onda_production_shaped_duplicate_still_disambiguates_by_duration(tmp_path) -> None:
    source = _source("ondacero", "ondacero", "Europe/Madrid", ONDA_SHOW_ID)
    edition_309 = _edition(source, "onda-309", ONDA_TITLE, duration_seconds=309)
    edition_251 = _edition(source, "onda-251", ONDA_TITLE, duration_seconds=251)
    store = _store(tmp_path)
    store.upsert_editions((edition_309, edition_251), observed_at=NOW)
    page = {
        "items": [
            _candidate(edition_309, source, uri=URI_309, duration_ms=309_000),
            _candidate(edition_309, source, uri=URI_251, duration_ms=251_000),
        ],
        "next": None,
    }

    outcomes = match_source_editions(
        FakeCatalogClient(pages=[page]),
        store,
        source,
        (edition_309, edition_251),
        now=NOW,
    ).outcomes

    assert [(outcome.spotify_episode_uri, outcome.spotify_duration_seconds) for outcome in outcomes] == [
        (URI_309, 309),
        (URI_251, 251),
    ]
'''

Path("tests/test_pr211_adversarial_regressions.py").write_text(regression_test)

engine_tests = r'''


def test_input_changed_ambiguity_preserves_cached_mapping_and_both_playlists(tmp_path: Path) -> None:
    clock = [NOW]

    class AmbiguousSpotify(_Spotify):
        ambiguous = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.ambiguous:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            self.show_calls += 1
            return catalogue_page(
                [
                    {
                        "uri": SER_URI,
                        "name": SER_TITLE,
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    },
                    {
                        "uri": "spotify:episode:" + "2" * 22,
                        "name": SER_TITLE,
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 61_000,
                    },
                ],
                offset=offset,
                limit=limit,
            )

    spotify = AmbiguousSpotify()
    runner = EngineRunner(
        _config("first", "second"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    writes_before = tuple(spotify.replacements)
    runner.planner_journal.observe_match_input(
        source_id="ser",
        source_native_id="ser-1",
        input_fingerprint="b" * 64,
        observed_at=NOW,
    )
    spotify.ambiguous = True
    clock[0] = NOW + timedelta(minutes=10)

    result = runner.run_cycle()

    assert result.ok
    assert result.sources[0].matching_diagnostics.matching_reason_input_changed_count == 1
    state = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert state is not None and state.status is MatchStatus.MATCHED
    assert state.spotify_episode_uri == SER_URI
    assert spotify.items["destination-first"] == [SER_URI]
    assert spotify.items["destination-second"] == [SER_URI]
    assert tuple(spotify.replacements) == writes_before
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert recovery is not None


def test_stale_match_input_inconclusive_catalogue_uses_six_hour_recovery_across_restart(
    tmp_path: Path,
) -> None:
    clock = [NOW]

    class TruncatedMissingSpotify(_Spotify):
        bounded = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.bounded:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            self.show_calls += 1
            items = [
                {
                    "uri": f"spotify:episode:{offset + index + 1:022d}",
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

    spotify = TruncatedMissingSpotify()
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
    assert spotify.show_calls == 1
    runner.planner_journal.observe_match_input(
        source_id="ser",
        source_native_id="ser-1",
        input_fingerprint="b" * 64,
        observed_at=NOW,
    )
    spotify.bounded = True

    clock[0] = NOW + timedelta(minutes=10)
    first = runner.run_cycle()
    assert first.ok
    assert spotify.show_calls == 3
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert recovery is not None
    calls_after_first = spotify.show_calls

    clock[0] = NOW + timedelta(minutes=20)
    assert runner.run_cycle().ok
    assert spotify.show_calls == calls_after_first

    clock[0] = NOW + timedelta(minutes=30)
    restarted = EngineRunner(
        _config("first"),
        SQLiteStore(store.path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert restarted.run_cycle().ok
    assert spotify.show_calls == calls_after_first

    clock[0] = NOW + timedelta(hours=6, minutes=10)
    due = restarted.run_cycle()
    assert due.ok
    assert spotify.show_calls == calls_after_first + 2
    assert due.sources[0].matching_diagnostics.matching_reason_evidence_recovery_count == 1


def test_new_input_breaks_matched_recovery_backoff_immediately(tmp_path: Path) -> None:
    clock = [NOW]

    class TruncatedMissingSpotify(_Spotify):
        bounded = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.bounded:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            self.show_calls += 1
            items = [
                {
                    "uri": f"spotify:episode:{offset + index + 1:022d}",
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

    spotify = TruncatedMissingSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    runner.planner_journal.observe_match_input(
        source_id="ser",
        source_native_id="ser-1",
        input_fingerprint="b" * 64,
        observed_at=NOW,
    )
    spotify.bounded = True
    clock[0] = NOW + timedelta(minutes=10)
    assert runner.run_cycle().ok
    calls_after_first = spotify.show_calls

    clock[0] = NOW + timedelta(minutes=20)
    assert runner.run_cycle().ok
    assert spotify.show_calls == calls_after_first

    runner.config = replace(
        runner.config,
        sources=(replace(_source(), spotify_release_delay_days=1),),
    )
    clock[0] = NOW + timedelta(minutes=30)
    changed = runner.run_cycle()

    assert changed.ok
    assert spotify.show_calls == calls_after_first + 2
    assert changed.sources[0].matching_diagnostics.matching_reason_input_changed_count == 1


def test_unknown_provider_duration_keeps_known_duration_and_enters_recovery_backoff(
    tmp_path: Path,
) -> None:
    clock = [NOW]

    class UnknownDurationSpotify(_Spotify):
        unknown_duration = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            page = super().show_episodes(show_id, limit=limit, offset=offset)
            if self.unknown_duration:
                page["items"][0]["duration_ms"] = None
            return page

    spotify = UnknownDurationSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    runner.planner_journal.clear_match_input("ser", "ser-1")
    spotify.unknown_duration = True
    calls_before = spotify.show_calls

    clock[0] = NOW + timedelta(minutes=10)
    first = runner.run_cycle()
    assert first.ok
    assert spotify.show_calls == calls_before + 1
    state = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert state is not None
    assert state.status is MatchStatus.MATCHED
    assert state.spotify_episode_uri == SER_URI
    assert state.spotify_duration_seconds == 60
    assert runner.planner_journal.get_matched_recovery("ser", "ser-1") is not None

    clock[0] = NOW + timedelta(minutes=20)
    assert runner.run_cycle().ok
    assert spotify.show_calls == calls_before + 1
'''

engine_test_path = Path("tests/test_engine.py")
engine_text = engine_test_path.read_text()
marker = "def test_input_changed_ambiguity_preserves_cached_mapping_and_both_playlists"
if marker in engine_text:
    raise RuntimeError("test_engine.py: PR211 regression tests already present")
engine_test_path.write_text(engine_text + engine_tests)

print("PR #211 adversarial fixes applied")
