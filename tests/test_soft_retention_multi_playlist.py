from __future__ import annotations

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
NOW = BASE + timedelta(hours=2)
AUTH_CONTEXT = "b" * 64
EXPIRY_SOURCE = SourceId("expiry-source")
ACTIVE_SOURCE = SourceId("active-source")
EXPIRY_PLAYLIST = PlaylistId("expiry-playlist")
ACTIVE_PLAYLIST = PlaylistId("active-playlist")
EXPIRY_OLD_URI = "spotify:episode:AAAAAAAAAAAAAAAAAAAAAA"
EXPIRY_CURRENT_URI = "spotify:episode:BBBBBBBBBBBBBBBBBBBBBB"
ACTIVE_CURRENT_URI = "spotify:episode:CCCCCCCCCCCCCCCCCCCCCC"
ACTIVE_NEW_URI = "spotify:episode:DDDDDDDDDDDDDDDDDDDDDD"


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
    def __init__(self) -> None:
        self.items = {
            "expiry-destination": [EXPIRY_CURRENT_URI, EXPIRY_OLD_URI],
            "active-destination": [ACTIVE_CURRENT_URI],
        }
        self.snapshots = {
            "expiry-destination": "expiry-snapshot",
            "active-destination": "active-snapshot",
        }
        self.show_calls: list[str] = []
        self.playlist_item_calls: list[str] = []
        self.snapshot_calls: list[str] = []
        self.replacements: list[tuple[str, tuple[str, ...]]] = []

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert limit == 50
        assert offset == 0
        self.show_calls.append(show_id)
        if show_id != "active-show":
            raise AssertionError("expiry-only source must not request catalogue work")
        candidates = (
            _candidate("Active current", "2026-09-13", ACTIVE_CURRENT_URI),
            _candidate("Active new", "2026-09-13", ACTIVE_NEW_URI),
        )
        return {
            "items": list(candidates),
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
        self.playlist_item_calls.append(playlist_id)
        items = self.items[playlist_id]
        page = items[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in page],
            "next": "next" if offset + len(page) < len(items) else None,
            "total": len(items),
        }

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        self.snapshot_calls.append(playlist_id)
        return {"snapshot_id": self.snapshots[playlist_id]}

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        self.items[playlist_id] = list(uris)
        self.replacements.append((playlist_id, tuple(uris)))
        snapshot = f"{playlist_id}-write-{len(self.replacements)}"
        self.snapshots[playlist_id] = snapshot
        return {"snapshot_id": snapshot}


def _candidate(title: str, release_date: str, uri: str) -> dict[str, object]:
    return {
        "name": title,
        "release_date": release_date,
        "release_date_precision": "day",
        "uri": uri,
        "duration_ms": 300_000,
    }


def _source(source_id: SourceId, *, endpoint: str, show_id: str) -> SourceDefinition:
    return SourceDefinition(
        id=source_id,
        display_name=str(source_id),
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url=endpoint,
        external_references=(
            ExternalReference(system="spotify", resource_type="show", external_id=show_id),
        ),
    )


def _playlist(
    playlist_id: PlaylistId,
    source_id: SourceId,
    destination: str,
) -> PlaylistDefinition:
    return PlaylistDefinition(
        id=playlist_id,
        display_name=str(playlist_id),
        description="",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((source_id,)),
        destination=DestinationReference(AdapterId("spotify"), destination),
        retention_hours=48,
    )


def _edition(
    source_id: SourceId,
    native_id: str,
    title: str,
    published_at: datetime,
) -> CanonicalEdition:
    return CanonicalEdition(
        source_id=source_id,
        source_native_id=native_id,
        title=title,
        published_at=published_at,
        edition_at=None,
        duration_seconds=None,
    )


def _rss(items: tuple[tuple[str, str, str], ...]) -> bytes:
    body = "".join(
        f"<item><guid>{native_id}</guid><title>{title}</title><pubDate>{published}</pubDate></item>"
        for native_id, title, published in items
    )
    return f"<rss><channel>{body}</channel></rss>".encode()


def _prepare_verified_baseline(
    store: SQLiteStore,
    source: SourceDefinition,
    playlist: PlaylistDefinition,
    editions_and_uris: tuple[tuple[CanonicalEdition, str], ...],
    *,
    snapshot_id: str,
) -> DestinationVerificationContext:
    editions = tuple(edition for edition, _uri in editions_and_uris)
    store.upsert_editions(editions, observed_at=BASE)
    journal = SpotifyPlannerJournal(store)
    for edition, uri in editions_and_uris:
        store.set_match_state(
            edition.source_id,
            edition.source_native_id,
            status=MatchStatus.MATCHED,
            spotify_episode_uri=uri,
            spotify_duration_seconds=300,
            updated_at=BASE,
        )
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
        source_timezones={source.id: source.timezone},
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
        snapshot_id=snapshot_id,
        desired_fingerprint=playlist_attestation_fingerprint(desired.uris),
        updated_at=BASE,
    )
    return expected


def test_new_match_in_one_playlist_does_not_wake_expiry_only_sibling(
    tmp_path: Path,
) -> None:
    expiry_source = _source(
        EXPIRY_SOURCE,
        endpoint="https://example.test/expiry.xml",
        show_id="expiry-show",
    )
    active_source = _source(
        ACTIVE_SOURCE,
        endpoint="https://example.test/active.xml",
        show_id="active-show",
    )
    expiry_playlist = _playlist(
        EXPIRY_PLAYLIST,
        EXPIRY_SOURCE,
        "expiry-destination",
    )
    active_playlist = _playlist(
        ACTIVE_PLAYLIST,
        ACTIVE_SOURCE,
        "active-destination",
    )
    config = EngineConfig(
        schema_version=1,
        sources=(expiry_source, active_source),
        playlists=(expiry_playlist, active_playlist),
    )
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()

    expiry_old = _edition(
        EXPIRY_SOURCE,
        "expiry-old",
        "Expiry old",
        BASE - timedelta(hours=47),
    )
    expiry_current = _edition(
        EXPIRY_SOURCE,
        "expiry-current",
        "Expiry current",
        BASE - timedelta(hours=1),
    )
    active_current = _edition(
        ACTIVE_SOURCE,
        "active-current",
        "Active current",
        BASE - timedelta(hours=1, minutes=30),
    )
    expiry_context = _prepare_verified_baseline(
        store,
        expiry_source,
        expiry_playlist,
        ((expiry_current, EXPIRY_CURRENT_URI), (expiry_old, EXPIRY_OLD_URI)),
        snapshot_id="expiry-snapshot",
    )
    active_context = _prepare_verified_baseline(
        store,
        active_source,
        active_playlist,
        ((active_current, ACTIVE_CURRENT_URI),),
        snapshot_id="active-snapshot",
    )

    expiry_feed = _rss(
        (
            ("expiry-old", "Expiry old", "Fri, 11 Sep 2026 21:00:00 +0000"),
            ("expiry-current", "Expiry current", "Sun, 13 Sep 2026 19:00:00 +0000"),
        )
    )
    active_feed = _rss(
        (
            ("active-current", "Active current", "Sun, 13 Sep 2026 18:30:00 +0000"),
            ("active-new", "Active new", "Sun, 13 Sep 2026 21:55:00 +0000"),
        )
    )

    def fetcher(url: str) -> bytes:
        if url.endswith("expiry.xml"):
            return expiry_feed
        if url.endswith("active.xml"):
            return active_feed
        raise AssertionError(f"unexpected URL {url}")

    spotify = _Spotify()
    auth = _Auth()
    runner = SoftRetentionEngineRunner(
        config,
        store,
        auth,
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
        monotonic_clock=lambda: 1_000.0,
    )
    runner.destination_leases.publish(
        expiry_playlist.id,
        expiry_context,
        verified_monotonic=1_000.0,
    )
    runner.destination_leases.publish(
        active_playlist.id,
        active_context,
        verified_monotonic=1_000.0,
    )

    result = runner.run_cycle()

    assert result.ok
    assert result.provider_session_created
    assert auth.calls == 1
    assert spotify.show_calls == ["active-show"]
    assert "expiry-destination" not in spotify.playlist_item_calls
    assert "expiry-destination" not in spotify.snapshot_calls
    assert all(destination != "expiry-destination" for destination, _uris in spotify.replacements)
    assert spotify.items["expiry-destination"] == [EXPIRY_CURRENT_URI, EXPIRY_OLD_URI]

    expiry_outcome = next(
        outcome for outcome in result.playlists if outcome.playlist_id == EXPIRY_PLAYLIST
    )
    active_outcome = next(
        outcome for outcome in result.playlists if outcome.playlist_id == ACTIVE_PLAYLIST
    )
    assert expiry_outcome.wrote is False
    assert expiry_outcome.desired_count == 1
    assert expiry_outcome.applied_count == 2
    assert active_outcome.wrote is True
    assert spotify.items["active-destination"] == [ACTIVE_NEW_URI, ACTIVE_CURRENT_URI]
    assert spotify.replacements == [
        ("active-destination", (ACTIVE_NEW_URI, ACTIVE_CURRENT_URI))
    ]
