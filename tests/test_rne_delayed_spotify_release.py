from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import (
    AdapterId,
    CanonicalEdition,
    CountryCode,
    DestinationReference,
    ExternalReference,
    LanguageTag,
    OrderingPolicy,
    ParserId,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.reconciliation import build_desired_state_from_store
from news_bulletin_playlist.registry import get_title_parser
from news_bulletin_playlist.spotify.matcher import match_source_editions


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
    *,
    spotify_release_delay_days: int = 0,
) -> SourceDefinition:
    return SourceDefinition(
        id=SourceId(source_id),
        display_name=source_id,
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo(timezone_name),
        enabled=True,
        parser_id=ParserId(parser_id),
        external_references=(
            ExternalReference(
                system="spotify",
                resource_type="show",
                external_id=show_id,
            ),
        ),
        spotify_release_delay_days=spotify_release_delay_days,
    )


def _rne_source(show_id: str) -> SourceDefinition:
    return _source(
        "rne",
        "rne",
        "Europe/Madrid",
        show_id,
        spotify_release_delay_days=1,
    )


def _edition(
    source: SourceDefinition,
    native_id: str,
    title: str,
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
        duration_seconds=60,
    )


def _candidate(
    *,
    title: str,
    release_date: str,
    uri: str = "spotify:episode:rne-delayed",
) -> dict[str, object]:
    return {
        "uri": uri,
        "name": title,
        "release_date": release_date,
        "release_date_precision": "day",
        "duration_ms": 60_000,
    }


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path / "state.sqlite3")
    store.initialize()
    return store


def _playlist(*source_ids: str) -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId("spain_spanish_news"),
        display_name="Noticias España",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection(tuple(SourceId(value) for value in source_ids)),
        destination=DestinationReference(AdapterId("spotify"), "playlist"),
        ordering=OrderingPolicy.EDITION_AT_DESC,
    )


def test_next_day_rne_release_matches_and_enters_semantic_chronology(tmp_path: Path) -> None:
    rne = _rne_source("0UgidTKsoaHiHDARuPQNW1")
    rne_21 = _edition(
        rne,
        "rne-20260901-2100",
        "NOTICIAS RNE - 01.09.2026 - 21.00 H",
    )
    store = _store(tmp_path)
    now = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
    store.upsert_editions((rne_21,), observed_at=now)

    client = FakeCatalogClient(
        pages=[
            {
                "items": [
                    _candidate(
                        title="NOTICIAS RNE - 01092026 - 2100H",
                        release_date="2026-09-02",
                    )
                ],
                "next": None,
            }
        ]
    )

    outcome = match_source_editions(client, store, rne, (rne_21,), now=now).outcomes[0]

    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == "spotify:episode:rne-delayed"
    assert "delayed Spotify release=+1d" in outcome.diagnostics

    ser_20 = CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id="ser-20260901-2000",
        title="SER 20:00",
        published_at=datetime(2026, 9, 1, 18, 2, tzinfo=UTC),
        edition_at=datetime(2026, 9, 1, 18, 0, tzinfo=UTC),
    )
    store.upsert_editions((ser_20,), observed_at=now)
    store.set_match_state(
        ser_20.source_id,
        ser_20.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:ser-2000",
        diagnostics="matched",
        updated_at=now,
    )

    desired = build_desired_state_from_store(
        store,
        _playlist("rne", "ser"),
        now=now,
    )

    assert desired.uris[:2] == (
        "spotify:episode:rne-delayed",
        "spotify:episode:ser-2000",
    )


def test_next_day_release_does_not_override_semantic_time_mismatch(tmp_path: Path) -> None:
    rne = _rne_source("show-rne")
    edition = _edition(
        rne,
        "rne-20260901-2100",
        "NOTICIAS RNE - 01.09.2026 - 21.00 H",
    )
    store = _store(tmp_path)
    now = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
    store.upsert_editions((edition,), observed_at=now)
    client = FakeCatalogClient(
        pages=[
            {
                "items": [
                    _candidate(
                        title="NOTICIAS RNE - 01.09.2026 - 19.00 H",
                        release_date="2026-09-02",
                    )
                ],
                "next": None,
            }
        ]
    )

    outcome = match_source_editions(client, store, rne, (edition,), now=now).outcomes[0]

    assert outcome.status is MatchStatus.PENDING
    assert outcome.spotify_episode_uri is None
    assert "semantic_time_mismatch=1" in outcome.diagnostics


def test_semantic_match_rejects_release_more_than_one_day_late(tmp_path: Path) -> None:
    rne = _rne_source("show-rne")
    edition = _edition(
        rne,
        "rne-20260901-2100",
        "NOTICIAS RNE - 01.09.2026 - 21.00 H",
    )
    store = _store(tmp_path)
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    store.upsert_editions((edition,), observed_at=now)
    client = FakeCatalogClient(
        pages=[
            {
                "items": [
                    _candidate(
                        title="NOTICIAS RNE - 01.09.2026 - 21.00 H",
                        release_date="2026-09-03",
                    )
                ],
                "next": None,
            }
        ]
    )

    outcome = match_source_editions(client, store, rne, (edition,), now=now).outcomes[0]

    assert outcome.status is MatchStatus.PENDING
    assert "release_date_skew_rejected=1" in outcome.diagnostics


def test_other_sources_do_not_inherit_rne_release_tolerance(tmp_path: Path) -> None:
    ser = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        ser,
        "ser-20260901-2100",
        "Las noticias de la SER, 21:00 (01/09/2026)",
    )
    store = _store(tmp_path)
    now = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
    store.upsert_editions((edition,), observed_at=now)
    client = FakeCatalogClient(
        pages=[
            {
                "items": [
                    _candidate(
                        title="Las noticias de la SER, 21:00 (01/09/2026)",
                        release_date="2026-09-02",
                        uri="spotify:episode:ser-next-day",
                    )
                ],
                "next": None,
            }
        ]
    )

    outcome = match_source_editions(client, store, ser, (edition,), now=now).outcomes[0]

    assert outcome.status is MatchStatus.PENDING
    assert "release_date_skew_rejected=1" in outcome.diagnostics


def test_missing_semantic_time_keeps_strict_release_date_matching(tmp_path: Path) -> None:
    source = _source("fallback", "ser", "Europe/Madrid", "show-fallback")
    edition = CanonicalEdition(
        source_id=source.id,
        source_native_id="fallback-native",
        title="Fallback bulletin",
        published_at=datetime(2026, 9, 1, 19, 0, tzinfo=UTC),
        edition_at=None,
    )
    store = _store(tmp_path)
    now = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
    store.upsert_editions((edition,), observed_at=now)
    client = FakeCatalogClient(
        pages=[
            {
                "items": [
                    _candidate(
                        title="Fallback bulletin",
                        release_date="2026-09-02",
                        uri="spotify:episode:fallback",
                    )
                ],
                "next": None,
            }
        ]
    )

    outcome = match_source_editions(client, store, source, (edition,), now=now).outcomes[0]

    assert outcome.status is MatchStatus.PENDING
    assert "release_date_mismatch=1" in outcome.diagnostics
