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
AUTH_CONTEXT = "c" * 64
SOURCE_ID = SourceId("source")
PLAYLIST_ID = PlaylistId("playlist")
OLD_URI = "spotify:episode:AAAAAAAAAAAAAAAAAAAAAA"
CURRENT_URI = "spotify:episode:BBBBBBBBBBBBBBBBBBBBBB"
RSS_URL = "https://example.test/news.xml"


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
        self.snapshot_calls = 0
        self.item_calls = 0
        self.show_calls = 0
        self.replacements: list[tuple[str, ...]] = []

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del show_id, limit, offset
        self.show_calls += 1
        raise AssertionError("restart audit must not inspect the catalogue")

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        assert playlist_id == "destination"
        self.snapshot_calls += 1
        return {"snapshot_id": "snapshot-a"}

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del playlist_id, limit, offset
        self.item_calls += 1
        raise AssertionError("unchanged snapshot must avoid playlist item reads")

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        del playlist_id
        self.replacements.append(tuple(uris))
        raise AssertionError("restart audit during grace must not write")


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


def _prepare_durable_baseline(
    store: SQLiteStore,
    source: SourceDefinition,
    playlist: PlaylistDefinition,
) -> None:
    old = _edition("old", "Old bulletin", BASE - timedelta(hours=47))
    current = _edition("current", "Current bulletin", BASE - timedelta(hours=1))
    store.upsert_editions((old, current), observed_at=BASE)
    journal = SpotifyPlannerJournal(store)
    for edition, uri in ((old, OLD_URI), (current, CURRENT_URI)):
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


def _feed() -> bytes:
    return b"""
    <rss><channel>
      <item><guid>old</guid><title>Old bulletin</title>
        <pubDate>Fri, 11 Sep 2026 21:00:00 +0000</pubDate></item>
      <item><guid>current</guid><title>Current bulletin</title>
        <pubDate>Sun, 13 Sep 2026 19:00:00 +0000</pubDate></item>
    </channel></rss>
    """


def test_restart_during_grace_uses_one_snapshot_audit_and_zero_put(tmp_path: Path) -> None:
    source = _source()
    playlist = _playlist()
    config = EngineConfig(schema_version=1, sources=(source,), playlists=(playlist,))
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    _prepare_durable_baseline(store, source, playlist)
    spotify = _Spotify()
    auth = _Auth()

    # Fresh runner: durable evidence exists, but the process-local destination lease does not.
    runner = SoftRetentionEngineRunner(
        config,
        store,
        auth,
        fetcher=lambda url: _feed() if url == RSS_URL else b"",
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
        monotonic_clock=lambda: 2_000.0,
    )

    result = runner.run_cycle()

    assert result.ok
    assert result.provider_work_planned
    assert result.provider_session_created
    assert auth.calls == 1
    assert spotify.show_calls == 0
    assert spotify.snapshot_calls == 1
    assert spotify.item_calls == 0
    assert spotify.replacements == []
    assert result.playlists[0].wrote is False
    assert result.playlists[0].desired_count == 1
    assert result.playlists[0].applied_count == 2
