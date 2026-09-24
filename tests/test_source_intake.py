from __future__ import annotations

import json
import sqlite3
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.managed_state import ManagedSource, ManagedState, ManagedStateStore
from news_bulletin_playlist.models import (
    CountryCode,
    EditorialScope,
    ExternalReference,
    LanguageTag,
    ParserId,
    SourceId,
    SourceOrigin,
)
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.source_intake import (
    SourceIntakeError,
    SourceIntakeFailure,
    inspect_spotify_source,
    normalize_spotify_show_locator,
)
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal
from news_bulletin_playlist.spotify_metrics import (
    SpotifyRequestMetrics,
    SpotifyWebApiOperation,
)

SHOW = "A" * 22
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
PARSER = ParserId("release_date_title")


class _Auth:
    def __init__(self) -> None:
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        self.calls += 1
        return "secret-access-token"


class _Client:
    def __init__(self, *, details: object | None = None, error: Exception | None = None) -> None:
        self.details = (
            {
                "id": SHOW,
                "type": "show",
                "uri": f"spotify:show:{SHOW}",
                "name": "News",
                "languages": ["es"],
                "total_episodes": 1,
            }
            if details is None
            else details
        )
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.pages: list[dict[str, Any]] = [catalogue_page([_episode()], total=1)]

    def show_details(self, show_id: str) -> dict[str, Any]:
        self.calls.append(("details", show_id))
        if self.error is not None:
            raise self.error
        assert isinstance(self.details, dict)
        return self.details

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.calls.append(("episodes", show_id))
        return self.pages[offset // 50]


def _episode(episode_id: str = "B" * 22, *, duration: object = 60_000) -> dict[str, Any]:
    return {
        "id": episode_id,
        "uri": f"spotify:episode:{episode_id}",
        "name": "Bulletin",
        "release_date": "2026-09-24",
        "release_date_precision": "day",
        "duration_ms": duration,
    }


def _inspect(
    tmp_path: Path,
    client: _Client,
    *,
    state: ManagedState | None = None,
    auth: _Auth | None = None,
    journal: SpotifyRateLimitJournal | None = None,
    parser_id: ParserId = PARSER,
):
    return inspect_spotify_source(
        SHOW,
        catalog=BUILTIN_CATALOG,
        state=state or ManagedState(),
        auth=auth or _Auth(),
        rate_limit=journal or SpotifyRateLimitJournal(SQLiteStore(tmp_path / "db")),
        now=NOW,
        parser_id=parser_id,
        client_factory=lambda token: client,
        clock=lambda: NOW,
    )


@pytest.mark.parametrize(
    "locator",
    [
        SHOW,
        f"spotify:show:{SHOW}",
        f"https://open.spotify.com/show/{SHOW}",
        f"https://open.spotify.com/show/{SHOW}?si=abc_123",
    ],
)
def test_supported_locators_converge(locator: str) -> None:
    assert normalize_spotify_show_locator(locator) == SHOW


@pytest.mark.parametrize(
    "locator",
    [
        "short",
        "A" * 513,
        f"spotify:episode:{SHOW}",
        f"spotify:show:{SHOW}x",
        f"http://open.spotify.com/show/{SHOW}",
        f"ftp://open.spotify.com/show/{SHOW}",
        f"https://evil.example/show/{SHOW}",
        f"https://open.spotify.com.evil.example/show/{SHOW}",
        f"https://user@open.spotify.com/show/{SHOW}",
        f"https://user:password@open.spotify.com/show/{SHOW}",
        f"https://open.spotify.com:443/show/{SHOW}",
        f"https://open.spotify.com/episode/{SHOW}",
        f"https://open.spotify.com/show/{SHOW}/extra",
        f"https://open.spotify.com/show/%41{'A' * 21}",
        f"https://open.spotify.com/show/{SHOW}/../x",
        f"https://open.spotify.com/show/{SHOW}#fragment",
        f"https://open.spotify.com/show/{SHOW}#",
        f"https://open.spotify.com/show/{SHOW}?",
        f"https://open.spotify.com/\nshow/{SHOW}",
        f"\x00https://open.spotify.com/show/{SHOW}",
        f"https://open.spotify.com/show/{SHOW}?foo=bar",
        f"https://open.spotify.com/show/{SHOW}?si=a&si=b",
        f"https://open.spotify.com/show/{SHOW}?si=a&foo=b",
        f"https://spotify.link/{SHOW}",
        f"https://open.spotify.com/show/{SHOW}?si=%2F",
        f"https://open.spotify.com/show/{SHOW}?s%69=abc",
        f"https://open.spotify.com/show/{SHOW}?si=%61bc",
    ],
)
def test_locator_rejects_ambiguous_or_unsafe_inputs(locator: str) -> None:
    with pytest.raises(SourceIntakeError) as raised:
        normalize_spotify_show_locator(locator)
    assert raised.value.failure is SourceIntakeFailure.INVALID_LOCATOR


def test_rejected_locator_cannot_trigger_auth_or_transport(tmp_path: Path) -> None:
    auth = _Auth()
    client = _Client()
    with pytest.raises(SourceIntakeError) as raised:
        inspect_spotify_source(
            f"https://attacker.example/show/{SHOW}",
            catalog=BUILTIN_CATALOG,
            state=ManagedState(),
            auth=auth,
            rate_limit=SpotifyRateLimitJournal(SQLiteStore(tmp_path / "db")),
            now=NOW,
            client_factory=lambda token: client,
            clock=lambda: NOW,
        )
    assert raised.value.failure is SourceIntakeFailure.INVALID_LOCATOR
    assert auth.calls == 0 and client.calls == []


def test_duplicate_built_in_and_custom_skip_auth_and_transport(tmp_path: Path) -> None:
    built_in = BUILTIN_CATALOG.sources[0].external_references[0].external_id
    custom = ManagedSource(
        id=SourceId(f"custom_{SHOW}"),
        spotify_show_id=SHOW,
        display_name="Custom",
        origin=SourceOrigin.country(CountryCode("ES")),
        language=LanguageTag("es"),
        editorial_scope=EditorialScope.INTERNATIONAL,
        timezone="UTC",
        parser_id=ParserId("release_date_title"),
        collection_filter_id=None,
        enabled=True,
    )
    auth = _Auth()
    client = _Client()
    journal = SpotifyRateLimitJournal(SQLiteStore(tmp_path / "db"))
    for locator, state in [(built_in, ManagedState()), (SHOW, ManagedState(sources=(custom,)))]:
        with pytest.raises(SourceIntakeError) as raised:
            inspect_spotify_source(
                locator,
                catalog=BUILTIN_CATALOG,
                state=state,
                auth=auth,
                rate_limit=journal,
                now=NOW,
                client_factory=lambda token: client,
                clock=lambda: NOW,
            )
        assert raised.value.failure is SourceIntakeFailure.DUPLICATE_SHOW
    assert auth.calls == 0
    assert client.calls == []


def test_future_built_in_local_id_collision_skips_transport(tmp_path: Path) -> None:
    future_source = replace(
        BUILTIN_CATALOG.sources[0],
        id=SourceId(f"custom_{SHOW}"),
        external_references=(ExternalReference("spotify", "show", "C" * 22),),
    )
    future_catalog = BuiltInCatalog(
        sources=BUILTIN_CATALOG.sources + (future_source,),
        playlists=BUILTIN_CATALOG.playlists,
    )
    auth = _Auth()
    client = _Client()
    with pytest.raises(SourceIntakeError) as raised:
        inspect_spotify_source(
            SHOW,
            catalog=future_catalog,
            state=ManagedState(),
            auth=auth,
            rate_limit=SpotifyRateLimitJournal(SQLiteStore(tmp_path / "db")),
            now=NOW,
            client_factory=lambda token: client,
            clock=lambda: NOW,
        )
    assert raised.value.failure is SourceIntakeFailure.DUPLICATE_SHOW
    assert auth.calls == 0 and client.calls == []


def test_success_is_bounded_and_has_no_source_state_side_effect(tmp_path: Path) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    store.save(ManagedState())
    before = store.path.read_bytes()
    client = _Client()
    client.pages = [catalogue_page([_episode(duration=None)], total=1)]
    result = _inspect(tmp_path, client)
    assert result.spotify_show_id == SHOW
    assert result.spotify_uri == f"spotify:show:{SHOW}"
    assert result.spotify_url == f"https://open.spotify.com/show/{SHOW}"
    assert result.language_hints == ("es",)
    assert result.publisher_hint is None
    assert not hasattr(result, "origin")
    assert not hasattr(result, "editorial_scope")
    assert not hasattr(result, "timezone")
    assert not hasattr(result, "language")
    assert result.coverage_proven and result.catalogue_complete
    assert result.catalogue_requests == 1
    assert result.accepted_episodes == 1
    assert result.first_seen_timestamp_count == 1
    assert result.title_timestamp_count == 0
    assert result.duration_missing_count == 1
    assert result.warnings
    assert client.calls == [("details", SHOW), ("episodes", SHOW)]
    assert store.path.read_bytes() == before
    assert store.load().sources == ()
    with sqlite3.connect(tmp_path / "db") as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not any(name.startswith("spotify_first_") for name in tables)


def test_complete_empty_catalogue_is_not_a_usable_source(tmp_path: Path) -> None:
    client = _Client()
    client.pages = [catalogue_page([], total=0)]

    with pytest.raises(SourceIntakeError) as raised:
        _inspect(tmp_path, client)

    assert raised.value.failure is SourceIntakeFailure.NO_USABLE_EPISODES
    assert client.calls == [("details", SHOW), ("episodes", SHOW)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "C" * 22),
        ("type", "episode"),
        ("uri", "spotify:show:" + "C" * 22),
        ("name", "  "),
        ("languages", [1]),
        ("languages", ["EN"]),
        ("publisher", 1),
        ("total_episodes", -1),
        ("total_episodes", True),
    ],
)
def test_malformed_show_hints_fail_closed(tmp_path: Path, field: str, value: object) -> None:
    details = _Client().details.copy()
    details[field] = value
    client = _Client(details=details)
    with pytest.raises(SourceIntakeError) as raised:
        _inspect(tmp_path, client)
    assert raised.value.failure is SourceIntakeFailure.MALFORMED_RESPONSE
    assert client.calls == [("details", SHOW)]


def test_bad_catalogue_and_insufficient_coverage(tmp_path: Path) -> None:
    for items, failure in [
        ([_episode("bad")], SourceIntakeFailure.MALFORMED_RESPONSE),
        ([_episode(), _episode()], SourceIntakeFailure.MALFORMED_RESPONSE),
    ]:
        client = _Client()
        client.pages = [catalogue_page(items, total=len(items))]
        with pytest.raises(SourceIntakeError) as raised:
            _inspect(tmp_path, client)
        assert raised.value.failure is failure

    client = _Client()
    client.pages = [
        catalogue_page(
            [_episode(f"{i:022d}") for i in range(page * 50, (page + 1) * 50)],
            offset=page * 50,
            next_url="https://api.spotify.com/next",
            total=201,
        )
        for page in range(4)
    ]
    with pytest.raises(SourceIntakeError) as raised:
        _inspect(tmp_path, client)
    assert raised.value.failure is SourceIntakeFailure.INSUFFICIENT_COVERAGE
    assert len(client.calls) == 5


def test_inconsistent_catalogue_paging_fails_closed(tmp_path: Path) -> None:
    client = _Client()
    client.pages = [catalogue_page([_episode()], next_url="https://api.spotify.com/next", total=1)]
    with pytest.raises(SourceIntakeError) as raised:
        _inspect(tmp_path, client)
    assert raised.value.failure is SourceIntakeFailure.MALFORMED_RESPONSE
    assert client.calls == [("details", SHOW), ("episodes", SHOW)]


@pytest.mark.parametrize(
    "error,failure",
    [
        (SpotifyApiError(401, "secret-provider-body"), SourceIntakeFailure.AUTHORIZATION),
        (SpotifyApiError(403, "secret-provider-body"), SourceIntakeFailure.FORBIDDEN),
        (SpotifyApiError(404, "secret-provider-body"), SourceIntakeFailure.PROVIDER_REJECTION),
        (
            SpotifyApiError(429, "secret-provider-body", retry_after=120),
            SourceIntakeFailure.RATE_LIMITED,
        ),
        (SpotifyApiError(503, "secret-provider-body"), SourceIntakeFailure.UNAVAILABLE),
        (SpotifyTransportError("secret-token"), SourceIntakeFailure.UNAVAILABLE),
        (SpotifyApiError(502, "invalid JSON response"), SourceIntakeFailure.MALFORMED_RESPONSE),
    ],
)
def test_provider_failures_are_sanitized_and_429_is_durable(
    tmp_path: Path,
    error: Exception,
    failure: SourceIntakeFailure,
) -> None:
    journal = SpotifyRateLimitJournal(SQLiteStore(tmp_path / "db"))
    client = _Client(error=error)
    with pytest.raises(SourceIntakeError) as raised:
        _inspect(tmp_path, client, journal=journal)
    assert raised.value.failure is failure
    assert "secret" not in str(raised.value)
    assert client.calls == [("details", SHOW)]
    if failure is SourceIntakeFailure.RATE_LIMITED:
        assert journal.active(now=NOW) is not None
        another = _Client()
        auth = _Auth()
        restarted_journal = SpotifyRateLimitJournal(SQLiteStore(tmp_path / "db"))
        with pytest.raises(SourceIntakeError) as suppressed:
            _inspect(tmp_path, another, auth=auth, journal=restarted_journal)
        assert suppressed.value.failure is SourceIntakeFailure.RATE_LIMITED
        assert auth.calls == 0 and another.calls == []


def test_journal_read_failure_after_auth_is_sanitized_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = SpotifyRateLimitJournal(SQLiteStore(tmp_path / "db"))
    auth = _Auth()
    client = _Client()
    active_calls = 0

    def fail_after_auth(*, now: datetime) -> None:
        nonlocal active_calls
        active_calls += 1
        if active_calls == 2:
            raise PersistenceError("secret-token /data/private.sqlite3: database is locked")
        return None

    monkeypatch.setattr(journal, "active", fail_after_auth)

    with pytest.raises(SourceIntakeError) as raised:
        _inspect(tmp_path, client, auth=auth, journal=journal)

    assert raised.value.failure is SourceIntakeFailure.UNAVAILABLE
    assert "secret-token" not in str(raised.value)
    assert "/data/private.sqlite3" not in str(raised.value)
    assert "database is locked" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert auth.calls == 1
    assert active_calls == 2
    assert client.calls == []


def test_journal_activation_failure_on_429_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = SpotifyRateLimitJournal(SQLiteStore(tmp_path / "db"))
    client = _Client(error=SpotifyApiError(429, "secret-provider-body", retry_after=120))

    def fail_activation(*, observed_at: datetime, retry_after_seconds: int | None) -> None:
        raise PersistenceError("secret-token /data/private.sqlite3: write failed")

    monkeypatch.setattr(journal, "activate", fail_activation)

    with pytest.raises(SourceIntakeError) as raised:
        _inspect(tmp_path, client, journal=journal)

    assert raised.value.failure is SourceIntakeFailure.UNAVAILABLE
    assert "secret-token" not in str(raised.value)
    assert "/data/private.sqlite3" not in str(raised.value)
    assert "write failed" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert client.calls == [("details", SHOW)]


def test_strategy_rejected_before_transport(tmp_path: Path) -> None:
    auth = _Auth()
    client = _Client()
    with pytest.raises(SourceIntakeError) as raised:
        _inspect(tmp_path, client, auth=auth, parser_id=ParserId("ser"))
    assert raised.value.failure is SourceIntakeFailure.STRATEGY
    assert auth.calls == 0 and client.calls == []


def test_custom_filter_is_not_exposed_to_intake(tmp_path: Path) -> None:
    auth = _Auth()
    client = _Client()
    with pytest.raises(SourceIntakeError) as raised:
        inspect_spotify_source(
            SHOW,
            catalog=BUILTIN_CATALOG,
            state=ManagedState(),
            auth=auth,
            rate_limit=SpotifyRateLimitJournal(SQLiteStore(tmp_path / "db")),
            now=NOW,
            collection_filter_id="un_news_es_minutes",
            client_factory=lambda token: client,
            clock=lambda: NOW,
        )
    assert raised.value.failure is SourceIntakeFailure.STRATEGY
    assert auth.calls == 0 and client.calls == []


def test_get_show_uses_known_endpoint_and_records_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requests: list[urllib.request.Request] = []

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps(_Client().details).encode()

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> _Response:
        requests.append(request)
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    metrics = SpotifyRequestMetrics(tmp_path / "metrics.db")
    client = SpotifyClient("secret-token", metrics=metrics)
    with metrics.request_scope() as scope:
        assert client.show_details(SHOW)["id"] == SHOW
        with pytest.raises(ValueError, match="canonical"):
            client.show_details("not-a-show-id")
    assert requests[0].full_url == f"https://api.spotify.com/v1/shows/{SHOW}"
    assert len(requests) == 1
    assert scope.snapshot().web_api_count(SpotifyWebApiOperation.SHOW_DETAILS) == 1
