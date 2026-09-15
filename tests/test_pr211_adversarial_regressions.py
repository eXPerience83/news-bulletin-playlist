from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from test_ondacero_duration_disambiguation import ONDA_SHOW_ID, ONDA_TITLE, URI_251, URI_309
from test_spotify_matcher import FakeCatalogClient, _candidate, _edition, _source, _store

import news_bulletin_playlist.spotify_planner as spotify_planner
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
    assert spotify_planner.source_contract_fingerprint(
        source
    ) == _legacy_source_contract_fingerprint(source)


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

    assert [
        (outcome.spotify_episode_uri, outcome.spotify_duration_seconds) for outcome in outcomes
    ] == [
        (URI_309, 309),
        (URI_251, 251),
    ]


def test_rejected_clock_rollback_repair_keeps_durable_mapping_and_never_qualifies(
    tmp_path,
) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        source,
        "ser-clock-rollback",
        "Las noticias de la SER, 10:00 (14/09/2026)",
        duration_seconds=60,
    )
    store = _store(tmp_path)
    _persist_cached_match(store, edition, uri=URI_309, duration=60)
    # Pin the durable row at NOW so the recovery observation at NOW-10m is rejected by
    # SQLite's monotonic updated_at guard. The shared helper deliberately stores older state.
    assert store.set_match_state(
        *edition.identity,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=URI_309,
        spotify_duration_seconds=60,
        diagnostics="known mapping at current durable time",
        updated_at=NOW,
    )

    stale_cached_candidate = _candidate(
        edition,
        source,
        title="contenido que no representa la edición",
        uri=URI_309,
        duration_ms=60_000,
    )
    replacement_candidate = _candidate(
        edition,
        source,
        uri=URI_251,
        duration_ms=61_000,
    )
    client = FakeCatalogClient(
        pages=[
            {
                "items": [stale_cached_candidate, replacement_candidate],
                "next": None,
            }
        ]
    )

    result = match_source_editions(
        client,
        store,
        source,
        (edition,),
        now=NOW - timedelta(minutes=10),
        evaluation_intents={edition.identity: MatchEvaluationIntent.EVIDENCE_RECOVERY},
    )
    outcome = result.outcomes[0]

    # The fresh catalogue positively contradicts A and uniquely selects B, but the temporal
    # persistence guard rejects the 09:20 repair because A was stored at 09:30. The public
    # result must therefore stay aligned with durable SQLite and must not publish positive
    # planner evidence for the rejected repair.
    assert outcome.from_cache
    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == URI_309
    assert outcome.spotify_duration_seconds == 60
    assert outcome.planner_input_evidence_qualified is False

    durable = store.get_match_state(*edition.identity)
    assert durable is not None
    assert durable.status is MatchStatus.MATCHED
    assert durable.spotify_episode_uri == URI_309
    assert durable.spotify_duration_seconds == 60
