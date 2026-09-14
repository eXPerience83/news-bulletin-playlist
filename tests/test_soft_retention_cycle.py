from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import (
    AdapterId,
    CanonicalEdition,
    CountryCode,
    DestinationReference,
    EngineConfig,
    ExternalReference,
    LanguageTag,
    ParserId,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.reconciliation import build_desired_state_from_store
from news_bulletin_playlist.soft_retention_engine import SoftRetentionEngineRunner
from news_bulletin_playlist.spotify_destination_audit import playlist_attestation_fingerprint
from news_bulletin_playlist.spotify_planner import (
    DestinationVerificationContext,
    SpotifyPlannerJournal,
    catalogue_context_fingerprint,
    destination_context_fingerprint,
    matching_input_fingerprint,
    playlist_config_fingerprint,
    stable_fingerprint,
)

BASE = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)
AUTH_CONTEXT = "a" * 64
SOURCE_ID = SourceId("source")
PLAYLIST_ID = PlaylistId("playlist")
OLD_URI = "spotify:episode:AAAAAAAAAAAAAAAAAAAAAA"
CURRENT_URI = "spotify:episode:BBBBBBBBBBBBBBBBBBBBBB"
NEW_URI = "spotify:episode:CCCCCCCCCCCCCCCCCCCCCC"
RSS_URL = "https://example.test/news.xml"


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _Auth:
    def __init__(self) -> None:
        self.calls = 0

    def planner_authorization_context(self) -> str:
        return AUTH_CONTEXT

    def get_access_token(self, *, now: datetime | None = None) -> str:
        assert now is not None
        self.calls += 1
        return "access-token"


class _Spotify:
    def __init__(self, physical_uris: tuple[str, ...]) -> None:
        self.items = list(physical_uris)
        self.snapshot = "snapshot-a"
        self.show_calls = 0
        self.item_calls = 0
        self.snapshot_calls = 0
        self.replacements: list[tuple[str, ...]] = []

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "show"
        assert limit == 50
        assert offset == 0
        self.show_calls += 1
        candidates = [
            _candidate("Old bulletin", "2026-09-11", OLD_URI),
            _candidate("Current bulletin", "2026-09-13", CURRENT_URI),
            _candidate("New bulletin", "2026-09-13", NEW_URI),
        ]
        return {
            "items": candidates,
            "next": None,
            "limit": limit,
            "offset": offset,
            "total": len(candidates),
        }

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert playlist_id == "destination"
        self.item_calls += 1
        selected = self.items[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in selected],
            "next": "next" if offset + len(selected) < len(self.items) else None,
            "total": len(self.items),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        assert playlist_id == "destination"
        self.items = list(uris)
        self.replacements.append(tuple(uris))
        self.snapshot = f"snapshot-write-{len(self.replacements)}"
        return {"snapshot_id": self.snapshot}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        assert playlist_id == "destination"
        self.snapshot_calls += 1
        return {"snapshot_id": self.snapshot}


def _candidate(title: str, release_date: str, uri: str) -> dict[str, object]:
    return {
        "name": title,
        "release_date": release_date,
        "release_date_precision": "day",
        "uri": uri,
        "duration_ms": 300_000,
    }


def _source() -> SourceDefinition:
    return SourceDefinition(
        id=SOURCE_ID,
        display_name="Source",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url=RSS_URL,
        external_references=(
            ExternalReference(system="spotify", resource_type="show", external_id="show"),
        ),
    )


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PLAYLIST_ID,
        display_name="Playlist",
        description="",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SOURCE_ID,)),
        destination=DestinationReference(AdapterId("spotify"), "destination"),
        retention_hours=48,
    )


def _edition(native_id: str, title: str, published_at: datetime) -> CanonicalEdition:
    return CanonicalEdition(
        source_id=SOURCE_ID,
        source_native_id=native_id,
        title=title,
        published_at=published_at,
        edition_at=None,
        duration_seconds=None,
    )


def _feed(include_new: bool) -> bytes:
    items = [
        ("old", "Old bulletin", "Fri, 11 Sep 2026 21:00:00 +0000"),
        ("current", "Current bulletin", "Sun, 13 Sep 2026 19:00:00 +0000"),
    ]
    if include_new:
        items.append(("new", "New bulletin", "Sun, 13 Sep 2026 21:55:00 +0000"))
    body = "".join(
        f"<item><guid>{native_id}</guid><title>{title}</title><pubDate>{published}</pubDate></item>"
        for native_id, title, published in items
    )
    return f"<rss><channel>{body}</channel></rss>".encode()


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def _prepare_verified_baseline(
    store: SQLiteStore,
    source: SourceDefinition,
    playlist: PlaylistDefinition,
) -> DestinationVerificationContext:
    old = _edition("old", "Old bulletin", BASE - timedelta(hours=47))
    current = _edition("current", "Current bulletin", BASE - timedelta(hours=1))
    store.upsert_editions((old, current), observed_at=BASE)
    for edition, uri in ((old, OLD_URI), (current, CURRENT_URI)):
        store.set_match_state(
            edition.source_id,
            edition.source_native_id,
            status=MatchStatus.MATCHED,
            spotify_episode_uri=uri,
            spotify_duration_seconds=300,
            updated_at=BASE,
        )

    journal = SpotifyPlannerJournal(store)
    for edition in (old, current):
        journal.observe_match_input(
            source_id=str(edition.source_id),
            source_native_id=edition.source_native_id,
            input_fingerprint=matching_input_fingerprint(source, edition),
            observed_at=BASE,
        )
    catalogue_context = catalogue_context_fingerprint(source, AUTH_CONTEXT)
    journal.verify_catalogue(catalogue_context, catalogue_context, verified_at=BASE)

    desired = build_desired_state_from_store(
        store,
        playlist,
        now=BASE,
        source_timezones={SOURCE_ID: source.timezone},
    )
    expected = DestinationVerificationContext(
        desired_fingerprint=stable_fingerprint(desired.uris),
        destination_context_fingerprint=destination_context_fingerprint(playlist),
        config_fingerprint=playlist_config_fingerprint(playlist),
        auth_context_fingerprint=AUTH_CONTEXT,
    )
    journal.observe_destination(
        playlist_id=str(playlist.id),
        destination_context_fingerprint=expected.destination_context_fingerprint,
        config_fingerprint=expected.config_fingerprint,
        auth_context_fingerprint=expected.auth_context_fingerprint,
        desired_fingerprint=expected.desired_fingerprint,
    )
    journal.verify_destination(str(playlist.id), expected=expected, verified_at=BASE)
    store.set_playlist_attestation(
        playlist.id,
        destination_id=playlist.destination.external_id,
        snapshot_id="snapshot-a",
        desired_fingerprint=playlist_attestation_fingerprint(desired.uris),
        updated_at=BASE,
    )
    return expected


def test_expiry_only_cycle_uses_zero_spotify_then_next_useful_write_cleans_up(
    tmp_path: Path,
) -> None:
    source = _source()
    playlist = _playlist()
    config = EngineConfig(schema_version=1, sources=(source,), playlists=(playlist,))
    store = _store(tmp_path)
    baseline_context = _prepare_verified_baseline(store, source, playlist)
    spotify = _Spotify((CURRENT_URI, OLD_URI))
    auth = _Auth()
    clock = _Clock(BASE + timedelta(hours=2))
    include_new = False

    def fetcher(url: str) -> bytes:
        assert url == RSS_URL
        return _feed(include_new)

    runner = SoftRetentionEngineRunner(
        config,
        store,
        auth,
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=clock,
        monotonic_clock=lambda: 1_000.0,
    )
    runner.destination_leases.publish(
        playlist.id,
        baseline_context,
        verified_monotonic=1_000.0,
    )

    expiry_only = runner.run_cycle()

    assert expiry_only.ok
    assert not expiry_only.provider_work_planned
    assert not expiry_only.provider_session_created
    assert auth.calls == 0
    assert spotify.show_calls == 0
    assert spotify.item_calls == 0
    assert spotify.snapshot_calls == 0
    assert spotify.replacements == []
    assert spotify.items == [CURRENT_URI, OLD_URI]
    assert expiry_only.playlists[0].desired_count == 1
    assert expiry_only.playlists[0].applied_count == 2
    assert expiry_only.playlists[0].wrote is False
    with sqlite3.connect(store.path) as connection:
        durable_counts = connection.execute(
            """
            SELECT desired_count, applied_count
            FROM playlist_runs
            WHERE playlist_id = ?
            ORDER BY finished_at DESC
            LIMIT 1
            """,
            (str(playlist.id),),
        ).fetchone()
    assert durable_counts == (1, 2)

    new = _edition("new", "New bulletin", BASE + timedelta(hours=1, minutes=55))
    store.upsert_editions((new,), observed_at=clock.value)
    store.set_match_state(
        new.source_id,
        new.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=NEW_URI,
        spotify_duration_seconds=300,
        updated_at=clock.value,
    )
    SpotifyPlannerJournal(store).observe_match_input(
        source_id=str(new.source_id),
        source_native_id=new.source_native_id,
        input_fingerprint=matching_input_fingerprint(source, new),
        observed_at=clock.value,
    )
    include_new = True
    clock.value += timedelta(minutes=10)

    useful_write = runner.run_cycle()

    assert useful_write.ok
    assert useful_write.provider_work_planned
    assert useful_write.provider_session_created
    assert auth.calls == 1
    assert spotify.replacements == [(NEW_URI, CURRENT_URI)]
    assert spotify.items == [NEW_URI, CURRENT_URI]
    assert useful_write.playlists[0].wrote is True
    assert useful_write.playlists[0].desired_count == 2
