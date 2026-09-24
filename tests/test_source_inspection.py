from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_state import ManagedSource, ManagedState, ManagedStateStore
from news_bulletin_playlist.models import (
    CountryCode,
    EditorialScope,
    LanguageTag,
    ParserId,
    SourceId,
    SourceOrigin,
)
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.source_inspection import (
    InspectionFailure,
    SourceInspectionError,
    inspect_configured_source,
    serialize_source_inspection,
)
from news_bulletin_playlist.source_intake import (
    SourceIntakeError,
    SourceIntakeFailure,
    inspect_spotify_source,
)
from news_bulletin_playlist.spotify.public_detector import (
    DETECTOR_SCHEMA_VERSION,
    PublicHttpResponse,
    PublicShowClient,
    PublicShowHttpError,
    PublicShowTransportError,
    spotify_show_id,
)
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
SHOW = "A" * 22


class _Auth:
    calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        self.calls += 1
        return "secret-oauth-token"


class _Client:
    def __init__(self, show: str = SHOW) -> None:
        self.show = show
        self.calls: list[str] = []
        self.pages: list[dict[str, Any]] = [catalogue_page([_episode()], total=1)]

    def show_details(self, show_id: str) -> dict[str, Any]:
        self.calls.append("details")
        return {
            "id": show_id,
            "type": "show",
            "uri": f"spotify:show:{show_id}",
            "name": "Bulletin",
            "languages": ["es"],
            "secret_unknown": "secret-provider-body",
        }

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.calls.append("episodes")
        return self.pages[offset // 50]


def _episode(
    episode_id: str = "B" * 22,
    *,
    title: str = "Bulletin",
    duration: int | None = 60_000,
    day: str = "2026-09-24",
) -> dict[str, Any]:
    return {
        "id": episode_id,
        "uri": f"spotify:episode:{episode_id}",
        "name": title,
        "release_date": day,
        "release_date_precision": "day",
        "duration_ms": duration,
        "secret_unknown": "secret-episode-body",
    }


def _custom(*, enabled: bool = True) -> ManagedSource:
    return ManagedSource(
        id=SourceId(f"custom_{SHOW}"),
        spotify_show_id=SHOW,
        display_name="Custom",
        origin=SourceOrigin.country(CountryCode("ES")),
        language=LanguageTag("es"),
        editorial_scope=EditorialScope.INTERNATIONAL,
        timezone="Europe/Madrid",
        parser_id=ParserId("release_date_title"),
        collection_filter_id=None,
        enabled=enabled,
    )


def _journal(tmp_path: Path) -> SpotifyRateLimitJournal:
    return SpotifyRateLimitJournal(SQLiteStore(tmp_path / "journal.sqlite3"))


def _configured(
    tmp_path: Path, source_id: SourceId, state: ManagedState, client: _Client, **kwargs: Any
):
    observe_public = "public_client" in kwargs
    return inspect_configured_source(
        source_id,
        catalog=BUILTIN_CATALOG,
        state=state,
        auth=_Auth(),
        rate_limit=_journal(tmp_path),
        now=NOW,
        client_factory=lambda token: client,
        observe_public=observe_public,
        clock=lambda: NOW,
        **kwargs,
    )


def test_candidate_and_configured_share_catalogue_evidence_and_duplicate_policy(
    tmp_path: Path,
) -> None:
    candidate_client = _Client()
    candidate = inspect_spotify_source(
        SHOW,
        catalog=BUILTIN_CATALOG,
        state=ManagedState(),
        auth=_Auth(),
        rate_limit=_journal(tmp_path),
        now=NOW,
        client_factory=lambda token: candidate_client,
        clock=lambda: NOW,
    )
    configured_client = _Client()
    state = ManagedState(sources=(_custom(),))
    configured = _configured(tmp_path, _custom().id, state, configured_client)
    assert candidate.ownership == "candidate" and candidate.source_id is None
    assert configured.ownership == "custom" and configured.timezone == "Europe/Madrid"
    assert candidate.episodes == configured.episodes
    assert candidate_client.calls == configured_client.calls == ["details", "episodes"]
    auth = _Auth()
    duplicate_client = _Client()
    with pytest.raises(SourceIntakeError) as raised:
        inspect_spotify_source(
            SHOW,
            catalog=BUILTIN_CATALOG,
            state=state,
            auth=auth,
            rate_limit=_journal(tmp_path),
            now=NOW,
            client_factory=lambda token: duplicate_client,
            clock=lambda: NOW,
        )
    assert raised.value.failure is SourceIntakeFailure.DUPLICATE_SHOW
    assert auth.calls == 0 and duplicate_client.calls == []


@pytest.mark.parametrize("enabled", [True, False])
def test_custom_audit_does_not_mutate_state(tmp_path: Path, enabled: bool) -> None:
    state = ManagedState(sources=(_custom(enabled=enabled),))
    store = ManagedStateStore(tmp_path / "managed-state.json")
    store.save(state)
    before = store.path.read_bytes()
    client = _Client()
    report = _configured(tmp_path, _custom().id, state, client)
    assert report.ownership == "custom"
    assert report.source_id == _custom().id
    assert report.accepted_episodes == 1
    assert store.path.read_bytes() == before
    assert state.sources[0].enabled is enabled
    with sqlite3.connect(tmp_path / "journal.sqlite3") as connection:
        table_rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in table_rows}
        assert connection.execute("SELECT count(*) FROM diagnostic_events").fetchone() == (0,)
    assert not any(
        name.startswith(("spotify_first_", "spotify_public_detector")) for name in tables
    )


def test_built_in_audit_uses_shipped_parser_and_filter(tmp_path: Path) -> None:
    source = next(item for item in BUILTIN_CATALOG.sources if item.collection_filter_id)
    show = spotify_show_id(source)
    client = _Client(show)
    client.pages = [
        catalogue_page(
            [
                _episode("B" * 22, title="Other product", duration=3_700_000),
                _episode(
                    "C" * 22,
                    title="La ONU en Minutos 24 de septiembre de 2026",
                    duration=902_000,
                ),
            ],
            total=2,
        )
    ]
    report = _configured(tmp_path, source.id, ManagedState(), client)
    assert report.ownership == "built_in"
    assert report.collection_filter_id == source.collection_filter_id
    assert report.filter_rejected_count == 1 and report.accepted_episodes == 1
    assert report.episodes[0].collection_filter_outcome == "rejected"
    assert report.episodes[0].parser_outcome == "not_evaluated"
    assert report.episodes[1].collection_filter_outcome == "accepted"
    assert [episode.catalogue_position for episode in report.episodes] == [1, 2]
    assert [episode.duration_seconds for episode in report.episodes] == [3700, 902]
    assert report.duration_buckets == (0, 0, 1, 0, 0)
    assert report.observed_duration_buckets == (0, 0, 1, 0, 1)
    assert sum(report.duration_buckets) == report.duration_known_count == 1
    assert (
        sum(report.observed_duration_buckets)
        == (report.observed_episodes - report.observed_duration_missing_count)
        == 2
    )
    encoded = json.loads(serialize_source_inspection(report))
    expected_keys = [
        "under_10m",
        "10_to_15m",
        "15_to_30m",
        "30_to_60m",
        "60m_plus",
    ]
    assert list(encoded["duration_buckets"]) == expected_keys
    assert list(encoded["observed_duration_buckets"]) == expected_keys
    assert encoded["episodes"][1]["duration_seconds"] == 902


def test_generic_can_report_all_items_rejected_by_shipped_filter(tmp_path: Path) -> None:
    source = next(item for item in BUILTIN_CATALOG.sources if item.collection_filter_id)
    client = _Client(spotify_show_id(source))
    client.pages = [catalogue_page([_episode(title="Other product")], total=1)]
    report = _configured(tmp_path, source.id, ManagedState(), client)
    assert report.coverage_proven and report.accepted_episodes == 0
    assert report.filter_rejected_count == 1
    assert report.episodes[0].parser_outcome == "not_evaluated"
    assert "no_usable_episodes" in report.warnings


def test_generic_reports_unproven_coverage_and_empty_catalogue_but_candidate_rejects(
    tmp_path: Path,
) -> None:
    empty = _Client()
    empty.pages = [catalogue_page([], total=0)]
    report = _configured(tmp_path, _custom().id, ManagedState(sources=(_custom(),)), empty)
    assert report.coverage_proven and report.accepted_episodes == 0
    assert "no_usable_episodes" in report.warnings
    with pytest.raises(SourceIntakeError) as raised:
        inspect_spotify_source(
            SHOW,
            catalog=BUILTIN_CATALOG,
            state=ManagedState(),
            auth=_Auth(),
            rate_limit=_journal(tmp_path),
            now=NOW,
            client_factory=lambda token: empty,
            clock=lambda: NOW,
        )
    assert raised.value.failure is SourceIntakeFailure.NO_USABLE_EPISODES
    incomplete = _Client()
    incomplete.pages = [
        catalogue_page(
            [_episode(f"{index:022d}") for index in range(page * 50, (page + 1) * 50)],
            offset=page * 50,
            total=201,
            next_url="https://api.spotify.com/next",
        )
        for page in range(4)
    ]
    report = _configured(tmp_path, _custom().id, ManagedState(sources=(_custom(),)), incomplete)
    assert not report.coverage_proven and report.observed_episodes == 200
    assert len(report.episodes) == 200 and report.catalogue_requests == 4
    assert "coverage_unproven" in report.warnings


def test_complete_two_page_report_keeps_received_positions(tmp_path: Path) -> None:
    client = _Client()
    client.pages = [
        catalogue_page(
            [_episode(f"{index:022d}") for index in range(50)],
            total=51,
            next_url="https://api.spotify.com/next",
        ),
        catalogue_page([_episode(f"{50:022d}")], offset=50, total=51),
    ]
    report = _configured(tmp_path, _custom().id, ManagedState(sources=(_custom(),)), client)
    assert report.catalogue_complete and report.coverage_proven
    assert report.catalogue_requests == 2
    assert report.observed_episodes == report.accepted_episodes == len(report.episodes) == 51
    assert [item.catalogue_position for item in report.episodes] == list(range(1, 52))


def test_same_day_fallback_ambiguity_is_evidence_not_candidate_failure(tmp_path: Path) -> None:
    client = _Client()
    client.pages = [
        catalogue_page(
            [
                _episode("B" * 22, duration=None),
                _episode("C" * 22, duration=3_700_000),
            ],
            total=2,
        )
    ]
    report = inspect_spotify_source(
        SHOW,
        catalog=BUILTIN_CATALOG,
        state=ManagedState(),
        auth=_Auth(),
        rate_limit=_journal(tmp_path),
        now=NOW,
        client_factory=lambda token: client,
        clock=lambda: NOW,
    )
    assert report.accepted_episodes == 2
    assert report.first_seen_timestamp_count == 2
    assert report.duration_missing_count == 1
    assert report.duration_buckets == (0, 0, 0, 0, 1)
    assert sum(report.duration_buckets) == report.duration_known_count
    assert (
        sum(report.observed_duration_buckets)
        == (report.observed_episodes - report.observed_duration_missing_count)
        == 1
    )
    assert "same_day_order_ambiguous" in report.warnings
    assert [item.ordering_evidence for item in report.episodes] == ["ambiguous", "ambiguous"]
    assert all(item.parser_outcome == "fallback" for item in report.episodes)


def test_long_title_is_bounded_only_in_report_not_in_catalogue_parser(tmp_path: Path) -> None:
    client = _Client()
    client.pages = [catalogue_page([_episode(title="X" * 1000)], total=1)]
    report = _configured(tmp_path, _custom().id, ManagedState(sources=(_custom(),)), client)
    assert report.episodes[0].title == "X" * 200
    assert report.episodes[0].title_truncated
    assert len(serialize_source_inspection(report)) < 5000


def test_exact_shipped_parser_reports_exact_edition(tmp_path: Path) -> None:
    source = next(item for item in BUILTIN_CATALOG.sources if str(item.parser_id) == "ser")
    client = _Client(spotify_show_id(source))
    client.pages = [
        catalogue_page(
            [
                _episode(title="Las noticias de la SER 12:00 (24/09/2026)"),
            ],
            total=1,
        )
    ]
    report = _configured(tmp_path, source.id, ManagedState(), client)
    assert report.episodes[0].parser_outcome == "exact"
    assert report.episodes[0].ordering_evidence == "exact"
    assert report.episodes[0].edition_at is not None


class _PublicTransport:
    def __init__(self, release_date: object) -> None:
        self.release_date = release_date

    def get(self, url: str, *, accept: str) -> PublicHttpResponse:
        payload = {
            "props": {
                "pageProps": {
                    "state": {
                        "data": {
                            "entity": {
                                "type": "episode",
                                "id": "B" * 22,
                                "uri": f"spotify:episode:{'B' * 22}",
                                "name": "Public bulletin",
                                "title": "Public bulletin",
                                "releaseDate": self.release_date,
                                "duration": 60_000,
                                "relatedEntityUri": f"spotify:show:{SHOW}",
                                "secret_unknown": "secret-public-body",
                            }
                        }
                    }
                }
            }
        }
        body = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        return PublicHttpResponse(200, "text/html", body.encode())


@pytest.mark.parametrize(
    "release_date,shape",
    [
        ("2026-09-24T12:00:00Z", "string"),
        ({"isoString": "2026-09-24T12:00:00Z"}, "object_iso_string"),
        (["secret-public-body"], "unsupported_array"),
    ],
)
def test_public_observation_uses_detector_parser_and_safe_serialization(
    tmp_path: Path,
    release_date: object,
    shape: str,
) -> None:
    report = _configured(
        tmp_path,
        _custom().id,
        ManagedState(sources=(_custom(),)),
        _Client(),
        public_client=PublicShowClient(_PublicTransport(release_date)),
    )
    assert report.public_status == "available"
    assert report.public_observation is not None
    assert report.public_observation.release_date_shape == shape
    assert report.public_observation.detector_schema_version == DETECTOR_SCHEMA_VERSION
    encoded = serialize_source_inspection(report)
    assert encoded == serialize_source_inspection(report)
    assert json.loads(encoded)["schema_version"] == 1
    for secret in (
        b"secret-oauth-token",
        b"secret-provider-body",
        b"secret-episode-body",
        b"secret-public-body",
    ):
        assert secret not in encoded


def test_candidate_can_explicitly_request_public_evidence(tmp_path: Path) -> None:
    report = inspect_spotify_source(
        SHOW,
        catalog=BUILTIN_CATALOG,
        state=ManagedState(),
        auth=_Auth(),
        rate_limit=_journal(tmp_path),
        now=NOW,
        client_factory=lambda token: _Client(),
        public_client=PublicShowClient(_PublicTransport("2026-09-24T12:00:00Z")),
        observe_public=True,
        clock=lambda: NOW,
    )
    assert report.ownership == "candidate"
    assert report.public_status == "available"
    assert report.public_observation is not None
    assert report.public_observation.release_date_shape == "string"


def test_public_failure_is_sanitized_and_does_not_fail_official_inspection(tmp_path: Path) -> None:
    class BrokenTransport:
        def get(self, url: str, *, accept: str) -> PublicHttpResponse:
            raise PublicShowTransportError("secret-public-body")

    report = _configured(
        tmp_path,
        _custom().id,
        ManagedState(sources=(_custom(),)),
        _Client(),
        public_client=PublicShowClient(BrokenTransport()),
    )
    assert report.public_status == "unavailable"
    assert report.public_observation is None
    assert "public_observation_unavailable" in report.warnings
    assert b"secret-public-body" not in serialize_source_inspection(report)


@pytest.mark.parametrize("failure", ["http", "schema"])
def test_public_http_or_schema_failure_is_optional_and_sanitized(
    tmp_path: Path,
    failure: str,
) -> None:
    class BrokenTransport:
        def get(self, url: str, *, accept: str) -> PublicHttpResponse:
            if failure == "http":
                raise PublicShowHttpError(503)
            return PublicHttpResponse(200, "text/html", b"secret-public-body")

    report = _configured(
        tmp_path,
        _custom().id,
        ManagedState(sources=(_custom(),)),
        _Client(),
        public_client=PublicShowClient(BrokenTransport()),
    )
    assert report.public_status == "unavailable"
    assert "public_observation_unavailable" in report.warnings
    assert b"secret-public-body" not in serialize_source_inspection(report)


def test_invalid_configured_strategy_fails_without_transport(tmp_path: Path) -> None:
    broken = replace(_custom(), parser_id=ParserId("not_shipped"))
    client = _Client()
    with pytest.raises(SourceInspectionError) as raised:
        _configured(tmp_path, broken.id, ManagedState(sources=(broken,)), client)
    assert raised.value.failure is InspectionFailure.CONFIGURATION
    assert client.calls == []
