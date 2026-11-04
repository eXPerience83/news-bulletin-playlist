"""Synthetic catalogue and privacy fixtures shared by offline validation suites."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.spotify.public_detector import spotify_show_id

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
SOURCE = BUILTIN_CATALOG.sources[0]
URL = "https://secret-external-url.test/secret-token"
SECRETS = (
    "secret-token",
    "secret-refresh-token",
    "Authorization",
    "secret-provider-body",
    "secret-title",
    "secret-episode-id",
    URL,
    "spotify:episode:",
    "secret-exception-string",
    "secret-rejected-value",
    "secret-provider-string",
    "secret-provider-array-entry",
    "secret-uri",
    "SecretNonJsonType",
)


def _item(position: int) -> dict[str, Any]:
    episode_id = f"{position:022d}"
    return {
        "id": episode_id,
        "uri": f"spotify:episode:{episode_id}",
        "name": "secret-title",
        "release_date": "2026-10-07",
        "release_date_precision": "day",
        "duration_ms": 60_000,
        "access_token": "secret-token",
        "refresh_token": "secret-refresh-token",
        "Authorization": "Bearer secret-token",
        "raw_body": "secret-provider-body",
        "external_urls": {"spotify": URL},
    }


class _Client:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, int]] = []

    def show_details(self, show_id: str) -> dict[str, Any]:
        self.calls.append(("details", 0))
        return {"id": show_id, "name": "secret-title", "raw_body": "secret-provider-body"}

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> Any:
        assert show_id == spotify_show_id(SOURCE)
        assert limit == 50
        self.calls.append(("episodes", offset))
        return self.pages[offset // 50]


class _Auth:
    def get_access_token(self, *, now: datetime | None = None) -> str:
        return "secret-token"


def _safe(serialized: str) -> None:
    for secret in (*SECRETS, *(f"{i:022d}" for i in range(97)), "Z" * 22):
        assert secret not in serialized
