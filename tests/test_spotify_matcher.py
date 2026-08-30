from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.models import (
    CanonicalEdition,
    CountryCode,
    ExternalReference,
    LanguageTag,
    ParserId,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.registry import get_title_parser
from news_bulletin_playlist.spotify.matcher import (
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_SIZE,
    match_source_editions,
)


@dataclass
class FakeCatalogClient:
    pages: list[dict[str, Any]]
    calls: list[tuple[str, int, int]] = field(default_factory=list)

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.calls.append((show_id, limit, offset))
        page_number = offset // limit
        if page_number < len(self.pages):
            return self.pages[page_number]
        return {"items": [], "next": None}


def _source(
    source_id: str,
    parser_id: str,
    timezone_name: str,
    show_id: str,
) -> SourceDefinition:
    country = "US" if source_id == "cnn" else "ES"
    return SourceDefinition(
        id=SourceId(source_id),
        display_name=source_id,
        countries=(CountryCode(country),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo(timezone_name),
        enabled=True,
        parser_id=ParserId(parser_id),
        external_references=(
            ExternalReference(system="spotify", resource_type="show", external_id=show_id),
        ),
    )


def _edition(
    source: SourceDefinition,
    native_id: str,
    title: str,
    *,
    duration_seconds: int | None = 60,
) -> CanonicalEdition:
    parsed = get_title_parser(str(source.parser_id)).parse(title)
    assert parsed is not None
    edition_at = (
        parsed.edition_at.replace(tzinfo=None)
        .replace(tzinfo=source.timezone)
        .astimezone(UTC)
    )
    return CanonicalEdition(
        source_id=source.id,
        source_native_id=native_id,
        title=title,
        published_at=edition_at + timedelta(minutes=2),
        edition_at=edition_at,
        duration_seconds=duration_seconds,
    )


def _candidate(
    edition: CanonicalEdition,
    source: SourceDefinition,
    *,
    title: str | None = None,
    uri: str = "spotify:episode:match",
    duration_ms: int = 65_000,
) -> dict[str, object]:
    assert edition.edition_at is not None
    return {
        "uri": uri,
        "name": title or edition.title,
        "release_date": edition.edition_at.astimezone(source.timezone).date().isoformat(),
        "release_date_precision": "day",
        "duration_ms": duration_ms,
    }


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path / "state.sqlite3")
    store.initialize()
    return store


@pytest.mark.parametrize(
    (
        "source_id",
        "timezone_name",
        "show_id",
        "source_title",
        "spotify_title",
    ),
    [
        (
            "ser",
            "Europe/Madrid",
            "4EwwdoHHYmbt49UXODQMpi",
            "Las noticias de la SER, 23:03 (29/08/2026)",
            "Las noticias de la SER 23:03 (29/08/2026)",
        ),
        (
            "rne",
            "Europe/Madrid",
            "0UgidTKsoaHiHDARuPQNW1",
            "NOTICIAS RNE - 29.08.2026 - 18.30 H",
            "NOTICIAS RNE - 29.08.2026 - 18,30 H",
        ),
        (
            "ondacero",
            "Europe/Madrid",
            "0tjEexypyczHXW9vE3SU3P",
            "Las noticias de Onda Cero de las 8:00h (29/8/2026)",
            "Las noticias de Onda Cero de las 8:00h (29/08/2026)",
        ),
        (
            "cnn",
            "America/New_York",
            "0vDgnorbpBr65YZzFVVouE",
            "CNN 5 cosas 08/29/2026 6 pm",
            "CNN 5 cosas 08/29/26 6pm",
        ),
    ],
)
def test_known_enabled_provider_cases_match_deterministically(
    tmp_path: Path,
    source_id: str,
    timezone_name: str,
    show_id: str,
    source_title: str,
    spotify_title: str,
) -> None:
    source = _source(source_id, source_id, timezone_name, show_id)
    edition = _edition(source, f"{source_id}-native", source_title)
    store = _store(tmp_path / source_id)
    store.upsert_editions((edition,), observed_at=datetime(2026, 8, 30, tzinfo=UTC))
    client = FakeCatalogClient(
        pages=[{"items": [_candidate(edition, source, title=spotify_title)], "next": None}]
    )

    result = match_source_editions(
        client,
        store,
        source,
        (edition,),
        now=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    assert result.catalogue_calls == 1
    assert client.calls == [(show_id, DEFAULT_PAGE_SIZE, 0)]
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == "spotify:episode:match"
    assert "duration delta=5s" in outcome.diagnostics
    assert store.get_spotify_episode_uri(edition.source_id, edition.source_native_id) == (
        "spotify:episode:match"
    )


def test_persisted_mapping_short_circuits_catalogue_lookup(tmp_path: Path) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(source, "ser-native", "Las noticias de la SER, 10:00 (30/08/2026)")
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    store.upsert_editions((edition,), observed_at=now)
    store.set_match_state(
        edition.source_id,
        edition.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:persisted",
        diagnostics="known durable mapping",
        updated_at=now - timedelta(days=1),
    )
    client = FakeCatalogClient(pages=[])

    result = match_source_editions(client, store, source, (edition,), now=now)

    assert result.catalogue_calls == 0
    assert client.calls == []
    assert result.outcomes[0].from_cache
    assert result.outcomes[0].spotify_episode_uri == "spotify:episode:persisted"


def test_multiple_viable_candidates_are_persisted_as_ambiguous(tmp_path: Path) -> None:
    source = _source("ondacero", "ondacero", "Europe/Madrid", "show-onda")
    edition = _edition(
        source,
        "onda-native",
        "Las noticias de Onda Cero de las 9:00h (30/8/2026)",
    )
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    store.upsert_editions((edition,), observed_at=now)
    first = _candidate(edition, source, uri="spotify:episode:first")
    second = _candidate(edition, source, uri="spotify:episode:second")
    client = FakeCatalogClient(pages=[{"items": [first, second], "next": None}])

    outcome = match_source_editions(client, store, source, (edition,), now=now).outcomes[0]

    assert outcome.status is MatchStatus.AMBIGUOUS
    assert outcome.spotify_episode_uri is None
    state = store.get_match_state(edition.source_id, edition.source_native_id)
    assert state is not None
    assert state.status is MatchStatus.AMBIGUOUS
    assert state.spotify_episode_uri is None
    assert state.diagnostics is not None
    assert "2 viable" in state.diagnostics


def test_missing_candidate_is_pending_and_retry_grace_avoids_churn(tmp_path: Path) -> None:
    source = _source("cnn", "cnn", "America/New_York", "show-cnn")
    edition = _edition(source, "cnn-native", "CNN 5 cosas 08/30/2026 6 am")
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    store.upsert_editions((edition,), observed_at=now)

    empty_client = FakeCatalogClient(pages=[{"items": [], "next": None}])
    first = match_source_editions(empty_client, store, source, (edition,), now=now)
    assert first.outcomes[0].status is MatchStatus.PENDING
    assert first.outcomes[0].spotify_episode_uri is None

    matching_page = {"items": [_candidate(edition, source)], "next": None}
    grace_client = FakeCatalogClient(pages=[matching_page])
    cached = match_source_editions(
        grace_client,
        store,
        source,
        (edition,),
        now=now + timedelta(minutes=10),
    )
    assert cached.catalogue_calls == 0
    assert cached.outcomes[0].status is MatchStatus.PENDING
    assert cached.outcomes[0].from_cache
    assert grace_client.calls == []

    retry_client = FakeCatalogClient(pages=[matching_page])
    retried = match_source_editions(
        retry_client,
        store,
        source,
        (edition,),
        now=now + timedelta(minutes=16),
    )
    assert retried.catalogue_calls == 1
    assert retried.outcomes[0].status is MatchStatus.MATCHED
    assert retried.outcomes[0].spotify_episode_uri == "spotify:episode:match"


def test_rne_duplicate_source_identities_remain_independent(tmp_path: Path) -> None:
    source = _source("rne", "rne", "Europe/Madrid", "show-rne")
    title = "NOTICIAS RNE - 30.08.2026 - 18.00 H"
    first = _edition(source, "rtve-asset-a", title)
    second = _edition(source, "rtve-asset-b", title)
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, 19, 0, tzinfo=UTC)
    store.upsert_editions((first, second), observed_at=now)
    client = FakeCatalogClient(
        pages=[{"items": [_candidate(first, source, uri="spotify:episode:rne-1800")], "next": None}]
    )

    result = match_source_editions(client, store, source, (first, second), now=now)

    assert [outcome.status for outcome in result.outcomes] == [
        MatchStatus.MATCHED,
        MatchStatus.MATCHED,
    ]
    assert first.identity != second.identity
    assert store.get_spotify_episode_uri(*first.identity) == "spotify:episode:rne-1800"
    assert store.get_spotify_episode_uri(*second.identity) == "spotify:episode:rne-1800"
    assert len(store.list_editions(source_id=source.id)) == 2


def test_catalogue_pagination_is_bounded_even_when_spotify_reports_more(tmp_path: Path) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(source, "ser-native", "Las noticias de la SER, 10:00 (30/08/2026)")
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    store.upsert_editions((edition,), observed_at=now)
    unrelated = {
        "uri": "spotify:episode:unrelated",
        "name": "Las noticias de la SER, 11:00 (30/08/2026)",
        "release_date": "2026-08-30",
        "release_date_precision": "day",
    }
    client = FakeCatalogClient(
        pages=[
            {"items": [unrelated], "next": "page-2"},
            {"items": [unrelated], "next": "page-3"},
            {"items": [_candidate(edition, source)], "next": None},
        ]
    )

    result = match_source_editions(client, store, source, (edition,), now=now)

    assert result.catalogue_calls == DEFAULT_MAX_PAGES
    assert client.calls == [
        ("show-ser", DEFAULT_PAGE_SIZE, 0),
        ("show-ser", DEFAULT_PAGE_SIZE, DEFAULT_PAGE_SIZE),
    ]
    assert result.outcomes[0].status is MatchStatus.PENDING


def test_max_pages_cannot_exceed_two_page_contract(tmp_path: Path) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(source, "ser-native", "Las noticias de la SER, 10:00 (30/08/2026)")
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    store.upsert_editions((edition,), observed_at=now)
    client = FakeCatalogClient(pages=[])

    with pytest.raises(ValueError, match="max_pages must be between 1 and 2"):
        match_source_editions(
            client,
            store,
            source,
            (edition,),
            now=now,
            max_pages=DEFAULT_MAX_PAGES + 1,
        )

    assert client.calls == []
