from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.spotify.probe import reconcile_playlist_items, run_catalog_probe
from news_bulletin_playlist.spotify.public_detector import spotify_show_id


@dataclass
class FakePlaylistClient:
    current: list[str] = field(default_factory=list)
    writes: int = 0
    reads: int = 0

    def playlist_items(
        self, playlist_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        self.reads += 1
        page = self.current[offset : offset + limit]
        has_more = offset + len(page) < len(self.current)
        return {
            "items": [{"item": {"uri": uri}} for uri in page],
            "next": "next-page" if has_more else None,
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, object]:
        self.writes += 1
        self.current = list(uris)
        return {}


def test_reconciliation_writes_only_when_desired_state_changes() -> None:
    client = FakePlaylistClient()
    desired = ["spotify:episode:one", "spotify:episode:two"]
    assert reconcile_playlist_items(client, "playlist", desired)
    assert client.current == desired
    assert client.writes == 1
    assert not reconcile_playlist_items(client, "playlist", desired)
    assert client.writes == 1


def test_reconciliation_detects_an_extra_item_after_first_100() -> None:
    desired = [f"spotify:episode:{n}" for n in range(100)]
    client = FakePlaylistClient(current=[*desired, "spotify:episode:extra"])
    assert reconcile_playlist_items(client, "playlist", desired)
    assert client.current == desired
    assert client.writes == 1
    assert client.reads == 3


def test_reconciliation_rejects_more_than_100_items_before_http_like_calls() -> None:
    client = FakePlaylistClient()
    with pytest.raises(ValueError):
        reconcile_playlist_items(client, "playlist", [f"spotify:episode:{n}" for n in range(101)])
    assert client.reads == 0
    assert client.writes == 0


@dataclass
class InvalidPlaylistClient(FakePlaylistClient):
    def playlist_items(
        self, playlist_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        self.reads += 1
        return {"items": None, "next": None}


def test_reconciliation_rejects_invalid_items_without_writing() -> None:
    client = InvalidPlaylistClient()
    with pytest.raises(RuntimeError, match="item list"):
        reconcile_playlist_items(client, "playlist", ["spotify:episode:one"])
    assert client.reads == 1
    assert client.writes == 0


@dataclass
class FakeCatalogClient:
    calls: list[tuple[str, int, int]] = field(default_factory=list)

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, object]:
        self.calls.append((show_id, limit, offset))
        source_id = next(
            str(source.id)
            for source in BUILTIN_CATALOG.sources
            if spotify_show_id(source) == show_id
        )
        if source_id == "rne":
            return {
                "items": [
                    {
                        "release_date": "2026-08-25",
                        "name": "NOTICIAS RNE - 25.08.2026 - 18.00 H",
                        "id": f"rne-{offset}",
                    }
                ],
                "next": "next-page" if offset < 150 else None,
            }
        return {
            "items": [{"release_date": "2026-08-29", "name": source_id, "id": source_id}],
            "next": "next-page",
        }

    def search_shows(self, query: str, *, limit: int = 10, offset: int = 0) -> dict[str, object]:
        return {"shows": {"items": []}}


def test_catalog_probe_only_uses_deep_paging_for_rne() -> None:
    client = FakeCatalogClient()
    assert run_catalog_probe(client) == 0
    counts = Counter(show_id for show_id, _, _ in client.calls)
    for source in BUILTIN_CATALOG.sources:
        expected = 4 if provider.source_id == "rne" else 1
        assert counts[spotify_show_id(source)] == expected


@dataclass
class InvalidCatalogClient(FakeCatalogClient):
    invalid_show_id: str = spotify_show_id(BUILTIN_CATALOG.sources[0])

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, object]:
        if show_id == self.invalid_show_id:
            self.calls.append((show_id, limit, offset))
            return {"items": None, "next": None}
        return super().show_episodes(show_id, limit=limit, offset=offset)


def test_catalog_probe_reports_invalid_items_as_failure() -> None:
    assert run_catalog_probe(InvalidCatalogClient()) == 1


@dataclass
class EmptyCatalogClient:
    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, object]:
        return {"items": [], "next": None}

    def search_shows(self, query: str, *, limit: int = 10, offset: int = 0) -> dict[str, object]:
        return {"shows": {"items": []}}


def test_catalog_probe_accepts_valid_empty_item_lists() -> None:
    assert run_catalog_probe(EmptyCatalogClient()) == 0
