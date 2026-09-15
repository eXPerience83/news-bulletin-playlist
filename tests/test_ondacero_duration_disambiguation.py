from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_spotify_matcher import FakeCatalogClient, _candidate, _edition, _source, _store

import news_bulletin_playlist.spotify_planner as spotify_planner
from news_bulletin_playlist.persistence import MatchStatus
from news_bulletin_playlist.spotify.matcher import match_source_editions
from news_bulletin_playlist.spotify.matching_policy import (
    duration_disambiguation_tolerance_seconds,
)

NOW = datetime(2026, 9, 15, 8, 30, tzinfo=UTC)
ONDA_TITLE = "Las noticias de Onda Cero de las 3:00h (14/9/2026)"
ONDA_SHOW_ID = "0tjEexypyczHXW9vE3SU3P"
URI_309 = "spotify:episode:" + "a" * 22
URI_251 = "spotify:episode:" + "b" * 22


def _onda():
    return _source("ondacero", "ondacero", "Europe/Madrid", ONDA_SHOW_ID)


def test_onda_duration_policy_is_narrow_source_opt_in() -> None:
    assert duration_disambiguation_tolerance_seconds(_onda()) == 1
    assert (
        duration_disambiguation_tolerance_seconds(
            _source("ser", "ser", "Europe/Madrid", "show-ser")
        )
        is None
    )
    assert (
        duration_disambiguation_tolerance_seconds(
            _source("ondacero", "ondacero", "Europe/Madrid", "other-show")
        )
        is None
    )


def test_onda_duplicate_semantic_assets_disambiguate_by_duration(tmp_path) -> None:
    source = _onda()
    first = _edition(source, "onda-309", ONDA_TITLE, duration_seconds=309)
    second = _edition(source, "onda-251", ONDA_TITLE, duration_seconds=251)
    store = _store(tmp_path)
    store.upsert_editions((first, second), observed_at=NOW)
    page = {
        "items": [
            _candidate(first, source, uri=URI_309, duration_ms=309_000),
            _candidate(first, source, uri=URI_251, duration_ms=251_000),
        ],
        "next": None,
    }

    result = match_source_editions(
        FakeCatalogClient(pages=[page]), store, source, (first, second), now=NOW
    )

    by_native = {outcome.edition.source_native_id: outcome for outcome in result.outcomes}
    assert by_native["onda-309"].status is MatchStatus.MATCHED
    assert by_native["onda-309"].spotify_episode_uri == URI_309
    assert by_native["onda-251"].status is MatchStatus.MATCHED
    assert by_native["onda-251"].spotify_episode_uri == URI_251
    assert "duration disambiguated" in by_native["onda-309"].diagnostics
    assert "duration disambiguated" in by_native["onda-251"].diagnostics


def test_duration_disambiguation_is_not_global(tmp_path) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        source,
        "ser-duplicate",
        "Las noticias de la SER, 10:00 (14/09/2026)",
        duration_seconds=251,
    )
    store = _store(tmp_path)
    store.upsert_editions((edition,), observed_at=NOW)
    page = {
        "items": [
            _candidate(edition, source, uri=URI_309, duration_ms=309_000),
            _candidate(edition, source, uri=URI_251, duration_ms=251_000),
        ],
        "next": None,
    }

    outcome = match_source_editions(
        FakeCatalogClient(pages=[page]), store, source, (edition,), now=NOW
    ).outcomes[0]

    assert outcome.status is MatchStatus.AMBIGUOUS
    assert outcome.spotify_episode_uri is None


def test_duration_disambiguation_requires_all_viable_durations(tmp_path) -> None:
    source = _onda()
    edition = _edition(source, "onda-missing-duration", ONDA_TITLE, duration_seconds=309)
    store = _store(tmp_path)
    store.upsert_editions((edition,), observed_at=NOW)
    missing_duration = _candidate(edition, source, uri=URI_251, duration_ms=251_000)
    missing_duration["duration_ms"] = None
    page = {
        "items": [
            _candidate(edition, source, uri=URI_309, duration_ms=309_000),
            missing_duration,
        ],
        "next": None,
    }

    outcome = match_source_editions(
        FakeCatalogClient(pages=[page]), store, source, (edition,), now=NOW
    ).outcomes[0]

    assert outcome.status is MatchStatus.AMBIGUOUS


def test_duration_disambiguation_requires_exactly_one_candidate_within_tolerance(
    tmp_path,
) -> None:
    source = _onda()
    edition = _edition(source, "onda-two-close", ONDA_TITLE, duration_seconds=251)
    store = _store(tmp_path)
    store.upsert_editions((edition,), observed_at=NOW)
    page = {
        "items": [
            _candidate(edition, source, uri=URI_309, duration_ms=250_000),
            _candidate(edition, source, uri=URI_251, duration_ms=252_000),
        ],
        "next": None,
    }

    outcome = match_source_editions(
        FakeCatalogClient(pages=[page]), store, source, (edition,), now=NOW
    ).outcomes[0]

    assert outcome.status is MatchStatus.AMBIGUOUS


def test_cached_uri_still_viable_but_ambiguous_is_inconclusive_not_false(tmp_path) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        source,
        "ser-cached",
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
        diagnostics="known durable mapping",
        updated_at=NOW - timedelta(hours=1),
    )
    page = {
        "items": [
            _candidate(edition, source, uri=URI_309, duration_ms=60_000),
            _candidate(edition, source, uri=URI_251, duration_ms=61_000),
        ],
        "next": None,
    }

    outcome = match_source_editions(
        FakeCatalogClient(pages=[page]), store, source, (edition,), now=NOW
    ).outcomes[0]

    assert outcome.from_cache
    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == URI_309
    assert outcome.planner_input_evidence_qualified is None


def test_cached_unique_uri_with_duration_drift_is_inconclusive(tmp_path) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        source,
        "ser-duration-drift",
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
        diagnostics="known durable mapping",
        updated_at=NOW - timedelta(hours=1),
    )
    page = {
        "items": [_candidate(edition, source, uri=URI_309, duration_ms=61_000)],
        "next": None,
    }

    outcome = match_source_editions(
        FakeCatalogClient(pages=[page]), store, source, (edition,), now=NOW
    ).outcomes[0]

    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == URI_309
    assert outcome.planner_input_evidence_qualified is None


def test_duration_policy_can_actively_disprove_cached_mapping(tmp_path) -> None:
    source = _onda()
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

    outcome = match_source_editions(
        FakeCatalogClient(pages=[page]), store, source, (edition,), now=NOW
    ).outcomes[0]

    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == URI_309
    assert outcome.planner_input_evidence_qualified is False


def test_duration_policy_is_bound_to_source_matching_contract(monkeypatch) -> None:
    source = _onda()
    before = spotify_planner.source_contract_fingerprint(source)
    monkeypatch.setattr(
        spotify_planner,
        "duration_disambiguation_tolerance_seconds",
        lambda _: 2,
    )
    after = spotify_planner.source_contract_fingerprint(source)
    assert before != after
