from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_ondacero_duration_disambiguation import ONDA_SHOW_ID, ONDA_TITLE, URI_251, URI_309
from test_spotify_matcher import FakeCatalogClient, _candidate, _edition, _source, _store

from news_bulletin_playlist.persistence import MatchStatus
from news_bulletin_playlist.spotify.matcher import MatchEvaluationIntent, match_source_editions

NOW = datetime(2026, 9, 15, 8, 30, tzinfo=UTC)


def _recover(client, store, source, edition):
    return match_source_editions(
        client,
        store,
        source,
        (edition,),
        now=NOW,
        evaluation_intents={edition.identity: MatchEvaluationIntent.EVIDENCE_RECOVERY},
    ).outcomes[0]


def test_evidence_recovery_reuses_onda_catalogue_to_replace_contradicted_mapping(tmp_path) -> None:
    source = _source("ondacero", "ondacero", "Europe/Madrid", ONDA_SHOW_ID)
    edition = _edition(source, "onda-stale", ONDA_TITLE, duration_seconds=251)
    store = _store(tmp_path)
    store.upsert_editions((edition,), observed_at=NOW)
    store.set_match_state(
        *edition.identity,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=URI_309,
        spotify_duration_seconds=309,
        diagnostics="old mapping",
        updated_at=NOW - timedelta(hours=1),
    )
    page = {
        "items": [
            _candidate(edition, source, uri=URI_309, duration_ms=309_000),
            _candidate(edition, source, uri=URI_251, duration_ms=251_000),
        ],
        "next": None,
    }

    outcome = _recover(FakeCatalogClient(pages=[page]), store, source, edition)

    assert not outcome.from_cache
    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == URI_251
    assert outcome.spotify_duration_seconds == 251
    assert outcome.planner_input_evidence_qualified is True
    persisted = store.get_match_state(*edition.identity)
    assert persisted is not None
    assert persisted.spotify_episode_uri == URI_251
    assert persisted.spotify_duration_seconds == 251


def test_evidence_recovery_reuses_catalogue_when_cached_uri_is_semantically_invalid(tmp_path) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        source,
        "ser-stale",
        "Las noticias de la SER, 10:00 (14/09/2026)",
        duration_seconds=60,
    )
    store = _store(tmp_path)
    store.upsert_editions((edition,), observed_at=NOW)
    store.set_match_state(
        *edition.identity,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=URI_309,
        spotify_duration_seconds=60,
        diagnostics="old mapping",
        updated_at=NOW - timedelta(hours=1),
    )
    page = {
        "items": [
            _candidate(
                edition,
                source,
                uri=URI_309,
                title="Unrelated programme",
                duration_ms=60_000,
            ),
            _candidate(edition, source, uri=URI_251, duration_ms=60_000),
        ],
        "next": None,
    }

    outcome = _recover(FakeCatalogClient(pages=[page]), store, source, edition)

    assert not outcome.from_cache
    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == URI_251
    assert outcome.planner_input_evidence_qualified is True
    persisted = store.get_match_state(*edition.identity)
    assert persisted is not None
    assert persisted.spotify_episode_uri == URI_251


def test_evidence_recovery_keeps_viable_cached_uri_when_result_is_still_ambiguous(tmp_path) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        source,
        "ser-ambiguous",
        "Las noticias de la SER, 10:00 (14/09/2026)",
        duration_seconds=60,
    )
    store = _store(tmp_path)
    store.upsert_editions((edition,), observed_at=NOW)
    store.set_match_state(
        *edition.identity,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=URI_309,
        spotify_duration_seconds=60,
        diagnostics="known mapping",
        updated_at=NOW - timedelta(hours=1),
    )
    page = {
        "items": [
            _candidate(edition, source, uri=URI_309, duration_ms=60_000),
            _candidate(edition, source, uri=URI_251, duration_ms=61_000),
        ],
        "next": None,
    }

    outcome = _recover(FakeCatalogClient(pages=[page]), store, source, edition)

    assert outcome.from_cache
    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == URI_309
    assert outcome.planner_input_evidence_qualified is None
    persisted = store.get_match_state(*edition.identity)
    assert persisted is not None
    assert persisted.spotify_episode_uri == URI_309


def test_evidence_recovery_refreshes_duration_for_same_unique_uri_without_second_cycle(
    tmp_path,
) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        source,
        "ser-duration-refresh",
        "Las noticias de la SER, 10:00 (14/09/2026)",
        duration_seconds=60,
    )
    store = _store(tmp_path)
    store.upsert_editions((edition,), observed_at=NOW)
    store.set_match_state(
        *edition.identity,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=URI_309,
        spotify_duration_seconds=60,
        diagnostics="known mapping",
        updated_at=NOW - timedelta(hours=1),
    )
    page = {
        "items": [_candidate(edition, source, uri=URI_309, duration_ms=61_000)],
        "next": None,
    }

    outcome = _recover(FakeCatalogClient(pages=[page]), store, source, edition)

    assert not outcome.from_cache
    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == URI_309
    assert outcome.spotify_duration_seconds == 61
    assert outcome.planner_input_evidence_qualified is True
    persisted = store.get_match_state(*edition.identity)
    assert persisted is not None
    assert persisted.spotify_duration_seconds == 61
