from __future__ import annotations

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
from news_bulletin_playlist.spotify.client import SpotifyTransportError
from news_bulletin_playlist.spotify.matcher import match_source_editions


def _source() -> SourceDefinition:
    return SourceDefinition(
        id=SourceId("ser"),
        display_name="Cadena SER",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ser"),
        external_references=(
            ExternalReference(system="spotify", resource_type="show", external_id="show-ser"),
        ),
    )


def _edition(source: SourceDefinition) -> CanonicalEdition:
    title = "Las noticias de la SER, 10:00 (30/08/2026)"
    parsed = get_title_parser("ser").parse(title)
    assert parsed is not None
    edition_at = (
        parsed.edition_at.replace(tzinfo=None)
        .replace(tzinfo=source.timezone)
        .astimezone(UTC)
    )
    return CanonicalEdition(
        source_id=source.id,
        source_native_id="ser-native",
        title=title,
        published_at=edition_at + timedelta(minutes=2),
        edition_at=edition_at,
    )


class FailingCatalogClient:
    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del show_id, limit, offset
        raise SpotifyTransportError("simulated catalogue outage")


class WrongReleaseDateClient:
    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del show_id, limit, offset
        return {
            "items": [
                {
                    "uri": "spotify:episode:wrong-date",
                    "name": "Las noticias de la SER, 10:00 (30/08/2026)",
                    "release_date": "2026-08-31",
                    "release_date_precision": "day",
                }
            ],
            "next": None,
        }


class CountingCatalogClient:
    def __init__(self) -> None:
        self.calls = 0

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del show_id, limit, offset
        self.calls += 1
        return {"items": [], "next": None}


def _prepared_store(tmp_path: Path, edition: CanonicalEdition) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.upsert_editions((edition,), observed_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC))
    return store


def test_catalogue_failure_does_not_persist_false_pending_state(tmp_path: Path) -> None:
    source = _source()
    edition = _edition(source)
    store = _prepared_store(tmp_path, edition)

    with pytest.raises(SpotifyTransportError, match="simulated catalogue outage"):
        match_source_editions(
            FailingCatalogClient(),
            store,
            source,
            (edition,),
            now=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        )

    assert store.get_match_state(edition.source_id, edition.source_native_id) is None


def test_matching_title_with_incompatible_release_date_stays_pending(tmp_path: Path) -> None:
    source = _source()
    edition = _edition(source)
    store = _prepared_store(tmp_path, edition)

    outcome = match_source_editions(
        WrongReleaseDateClient(),
        store,
        source,
        (edition,),
        now=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    ).outcomes[0]

    assert outcome.status is MatchStatus.PENDING
    assert outcome.spotify_episode_uri is None
    assert store.get_spotify_episode_uri(edition.source_id, edition.source_native_id) is None


def test_future_retry_state_is_reused_until_matcher_clock_catches_up(tmp_path: Path) -> None:
    source = _source()
    edition = _edition(source)
    store = _prepared_store(tmp_path, edition)
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    store.set_match_state(
        edition.source_id,
        edition.source_native_id,
        status=MatchStatus.PENDING,
        diagnostics="future persisted state",
        updated_at=now + timedelta(minutes=5),
    )
    client = CountingCatalogClient()

    result = match_source_editions(client, store, source, (edition,), now=now)

    assert result.catalogue_calls == 0
    assert client.calls == 0
    assert result.outcomes[0].status is MatchStatus.PENDING
    assert result.outcomes[0].from_cache
