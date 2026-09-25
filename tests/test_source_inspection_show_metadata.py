from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_state import ManagedState
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.source_intake import (
    SourceIntakeError,
    SourceIntakeFailure,
    inspect_spotify_source,
)
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal

SHOW = "A" * 22
NOW = datetime(2026, 9, 25, 10, tzinfo=UTC)


class _Auth:
    def get_access_token(self, *, now: datetime | None = None) -> str:
        return "secret-oauth-token"


class _Client:
    def __init__(self, details: object) -> None:
        self.details = details
        self.calls: list[str] = []

    def show_details(self, show_id: str) -> dict[str, Any]:
        self.calls.append("details")
        assert isinstance(self.details, dict)
        return self.details

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.calls.append("episodes")
        assert show_id == SHOW
        assert limit == 50 and offset == 0
        return catalogue_page(
            [
                {
                    "id": "B" * 22,
                    "uri": f"spotify:episode:{'B' * 22}",
                    "name": "Bulletin",
                    "release_date": "2026-09-25",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
            ],
            total=1,
        )


def _journal(tmp_path: Path) -> SpotifyRateLimitJournal:
    return SpotifyRateLimitJournal(SQLiteStore(tmp_path / "db.sqlite3"))


def _inspect(
    tmp_path: Path,
    details: object,
    journal: SpotifyRateLimitJournal | None = None,
):
    client = _Client(details)
    report = inspect_spotify_source(
        SHOW,
        catalog=BUILTIN_CATALOG,
        state=ManagedState(),
        auth=_Auth(),
        rate_limit=journal or _journal(tmp_path),
        now=NOW,
        client_factory=lambda token: client,
        clock=lambda: NOW,
    )
    return report, client


def test_development_mode_metadata_hints_do_not_block_catalogue(tmp_path: Path) -> None:
    report, client = _inspect(
        tmp_path,
        {
            "id": SHOW,
            "name": "N" * 240,
            "languages": ["es-419"],
            "publisher": None,
            "total_episodes": None,
        },
    )

    assert client.calls == ["details", "episodes"]
    assert report.display_name_hint == "N" * 120
    assert report.language_hints == ("es-419",)
    assert report.publisher_hint is None
    assert report.total_episodes_hint is None
    assert report.accepted_episodes == 1


def test_missing_languages_are_an_empty_hint(tmp_path: Path) -> None:
    report, client = _inspect(
        tmp_path,
        {
            "id": SHOW,
            "type": "show",
            "uri": f"spotify:show:{SHOW}",
            "name": "Bulletin",
            "languages": None,
        },
    )

    assert client.calls == ["details", "episodes"]
    assert report.language_hints == ()


def test_contradictory_identity_fails_before_catalogue_with_safe_diagnostic(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    client = _Client(
        {
            "id": "C" * 22,
            "type": "show",
            "uri": f"spotify:show:{SHOW}",
            "name": "secret-provider-name",
            "languages": ["es"],
        }
    )

    with pytest.raises(SourceIntakeError) as raised:
        inspect_spotify_source(
            SHOW,
            catalog=BUILTIN_CATALOG,
            state=ManagedState(),
            auth=_Auth(),
            rate_limit=journal,
            now=NOW,
            client_factory=lambda token: client,
            clock=lambda: NOW,
        )

    assert raised.value.failure is SourceIntakeFailure.MALFORMED_RESPONSE
    assert client.calls == ["details"]
    diagnostics = journal.diagnostics
    assert diagnostics is not None
    store = diagnostics.store
    assert store is not None
    events = store.list_events(limit=10)
    event = next(
        item for item in events if item.event_name == "source_inspection_show_identity_invalid"
    )
    assert event.details == {
        "failure_class": "response_shape_error",
        "phase": "configuration",
    }
    rendered = str(event.details)
    assert "secret-provider-name" not in rendered
    assert "secret-oauth-token" not in rendered
