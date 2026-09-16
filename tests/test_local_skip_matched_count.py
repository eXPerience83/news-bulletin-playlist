from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.engine import EngineRunner
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

NOW = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
SOURCE_ID = SourceId("ser")
CURRENT_URI = "spotify:episode:0000000000000000000001"
HISTORICAL_URI = "spotify:episode:0000000000000000000002"
CURRENT_TITLE = "Las noticias de la SER, 10:00 (16/09/2026)"


class _Auth:
    def __init__(self) -> None:
        self.calls = 0

    def planner_authorization_context(self) -> str:
        return "a" * 64

    def get_access_token(self, *, now: datetime | None = None) -> str:
        assert now is not None
        self.calls += 1
        return "access-token"


class _Spotify:
    def __init__(self) -> None:
        self.show_calls = 0
        self.playlist_reads = 0
        self.replacements = 0
        self.items: list[str] = []
        self.snapshot_id = "snapshot-initial"

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "ser-show"
        self.show_calls += 1
        return catalogue_page(
            [
                {
                    "uri": CURRENT_URI,
                    "name": CURRENT_TITLE,
                    "release_date": "2026-09-16",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
            ],
            offset=offset,
            limit=limit,
        )

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert playlist_id == "playlist"
        self.playlist_reads += 1
        selected = self.items[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in selected],
            "next": None,
            "total": len(self.items),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        assert playlist_id == "playlist"
        self.replacements += 1
        self.items = list(uris)
        self.snapshot_id = f"snapshot-{self.replacements}"
        return {"snapshot_id": self.snapshot_id}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        assert playlist_id == "playlist"
        return {"snapshot_id": self.snapshot_id}


def _config() -> EngineConfig:
    source = SourceDefinition(
        id=SOURCE_ID,
        display_name="SER",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ser"),
        endpoint_url="https://example.test/ser.xml",
        external_references=(
            ExternalReference(
                system="spotify",
                resource_type="show",
                external_id="ser-show",
            ),
        ),
    )
    playlist = PlaylistDefinition(
        id=PlaylistId("playlist"),
        display_name="playlist",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SOURCE_ID,)),
        destination=DestinationReference(
            adapter_id=AdapterId("spotify"),
            external_id="playlist",
        ),
        retention_hours=48,
    )
    return EngineConfig(schema_version=1, sources=(source,), playlists=(playlist,))


def _rss() -> bytes:
    return (
        "<rss><channel><item>"
        "<guid>current</guid>"
        f"<title>{CURRENT_TITLE}</title>"
        "<pubDate>Wed, 16 Sep 2026 07:05:00 +0000</pubDate>"
        "</item></channel></rss>"
    ).encode()


def test_zero_provider_cycle_keeps_matched_count_scoped_to_matching_window(
    tmp_path: Path,
) -> None:
    current = NOW
    auth = _Auth()
    spotify = _Spotify()
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    runner = EngineRunner(
        _config(),
        store,
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )

    initial = runner.run_cycle()
    assert initial.ok
    assert initial.sources[0].matched_count == 1

    # Keep a durable historical match in the 30-day canonical store while placing it
    # outside the active 48-hour matching window. It must not change source-cycle
    # diagnostics merely because the next cycle can skip Spotify entirely.
    current = NOW + timedelta(minutes=10)
    historical = CanonicalEdition(
        source_id=SOURCE_ID,
        source_native_id="historical",
        title="Las noticias de la SER, 10:00 (13/09/2026)",
        published_at=current - timedelta(hours=72),
        edition_at=current - timedelta(hours=72),
        duration_seconds=60,
    )
    store.upsert_editions((historical,), observed_at=current)
    assert store.set_match_state(
        SOURCE_ID,
        historical.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=HISTORICAL_URI,
        spotify_duration_seconds=60,
        updated_at=current,
    )

    auth_calls = auth.calls
    show_calls = spotify.show_calls
    playlist_reads = spotify.playlist_reads
    replacements = spotify.replacements

    skipped = runner.run_cycle()

    assert skipped.ok
    assert skipped.provider_work_planned is False
    assert skipped.provider_session_created is False
    assert skipped.sources[0].matching_diagnostics.matching_work_count == 0
    assert skipped.sources[0].matched_count == initial.sources[0].matched_count == 1
    assert auth.calls == auth_calls
    assert spotify.show_calls == show_calls
    assert spotify.playlist_reads == playlist_reads
    assert spotify.replacements == replacements
