from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _spotify_catalogue import catalogue_page

import news_bulletin_playlist.engine as engine_module
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine import (
    EngineRunner,
    PlaylistCycleOutcome,
    SourceCycleOutcome,
)
from news_bulletin_playlist.models import (
    AdapterId,
    DestinationReference,
    EngineConfig,
    PlaylistDefinition,
    PlaylistId,
    SourceSelection,
)
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.spotify.client import SpotifyApiError
from news_bulletin_playlist.spotify.first_desired import desired_fingerprint
from news_bulletin_playlist.spotify.first_models import (
    RefreshReason,
    SpotifyFirstEpisode,
    TimestampSource,
)
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetectorCycleSummary,
    PublicDetectorJournal,
    spotify_show_id,
)
from news_bulletin_playlist.spotify_backoff import ProviderAuthorizationSession

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
EPISODE_URI = "spotify:episode:" + "E" * 22


class _EpisodeJournal:
    def __init__(self) -> None:
        self.episodes = ("persisted-old",)
        self.full_reads = 0

    def list_episodes(self) -> tuple[str, ...]:
        self.full_reads += 1
        return self.episodes

    def prune(self, *, before: datetime) -> None:
        del before

    def prune_history(self, *, now: datetime) -> None:
        del now


class _Auth:
    def authorization_context(self) -> str:
        return "context"

    def acquire_provider_session(
        self,
        *,
        now: datetime | None = None,
    ) -> ProviderAuthorizationSession:
        del now
        return ProviderAuthorizationSession("token", "context")


def _source_outcome() -> SourceCycleOutcome:
    return SourceCycleOutcome(
        source_id="shared-source",  # type: ignore[arg-type]
        ok=True,
        episode_count=1,
        refreshed=False,
        refresh_reason=None,
        api_request_count=0,
        last_detector_success_at=NOW,
        last_catalogue_refresh_at=NOW,
        detector_ok=True,
        detector_changed=False,
    )


def _playlist_outcome(
    playlist_id: str,
    *,
    stale_escalation: bool,
    ok: bool = True,
) -> PlaylistCycleOutcome:
    return PlaylistCycleOutcome(
        playlist_id=playlist_id,  # type: ignore[arg-type]
        ok=ok,
        desired_count=1,
        applied_count=1 if ok else None,
        wrote=False if ok else None,
        last_success_at=NOW if ok else None,
        stale_escalation=stale_escalation,
    )


def _runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stale_first: bool,
    failed_first: bool = False,
) -> tuple[EngineRunner, _EpisodeJournal, dict[str, tuple[str, ...]]]:
    source = SimpleNamespace(id="shared-source", enabled=True)
    playlists = (
        SimpleNamespace(
            id="playlist-a",
            enabled=True,
            source_selection=SimpleNamespace(explicit=(source.id,)),
        ),
        SimpleNamespace(
            id="playlist-b",
            enabled=True,
            source_selection=SimpleNamespace(explicit=(source.id,)),
        ),
    )
    journal = _EpisodeJournal()
    builds: dict[str, tuple[str, ...]] = {}

    def build_desired(
        playlist: SimpleNamespace,
        episodes: tuple[str, ...],
        *,
        sources: dict[str, SimpleNamespace],
        now: datetime,
    ) -> SimpleNamespace:
        del sources, now
        builds[playlist.id] = tuple(episodes)
        desired = SimpleNamespace(uris=(EPISODE_URI,), items=(object(),))
        return SimpleNamespace(desired=desired, duration_unavailable_count=0)

    monkeypatch.setattr(engine_module, "build_spotify_first_desired_state", build_desired)

    runner = object.__new__(EngineRunner)
    runner.config = SimpleNamespace(  # type: ignore[assignment]
        sources=(source,), playlists=playlists
    )
    runner.clock = lambda: NOW
    runner.public_detector = SimpleNamespace(observe_sources=lambda *args, **kwargs: object())
    runner.first_journal = journal  # type: ignore[assignment]
    runner.auth = _Auth()  # type: ignore[assignment]
    runner.client_factory = lambda _token: object()  # type: ignore[assignment]
    runner._refresh_plan = lambda _detector, *, now: ()  # type: ignore[method-assign]
    runner._unchanged_source_outcome = (  # type: ignore[method-assign]
        lambda _source_id, _detector, *, now: _source_outcome()
    )
    runner._playlist_sources_ready = lambda _playlist, *, now: True  # type: ignore[method-assign]
    runner._destination_skip_evidence = (  # type: ignore[method-assign]
        lambda _playlist, _fingerprint, *, now: None
    )

    def reconcile(
        playlist: SimpleNamespace,
        _build: SimpleNamespace,
        _fingerprint: str,
        _client: object,
        _session: ProviderAuthorizationSession,
        *,
        now: datetime,
    ) -> PlaylistCycleOutcome:
        del now
        is_first = playlist.id == "playlist-a"
        stale_escalation = stale_first and is_first
        failed = failed_first and is_first
        if stale_escalation or failed:
            journal.episodes = ("persisted-refreshed",)
        return _playlist_outcome(
            playlist.id,
            stale_escalation=stale_escalation,
            ok=not failed,
        )

    runner._reconcile_playlist = reconcile  # type: ignore[method-assign]
    return runner, journal, builds


def test_ordinary_reconciliation_does_not_reload_full_episode_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, journal, builds = _runner(monkeypatch, stale_first=False)

    result = runner._run_cycle()

    assert result.ok is True
    assert journal.full_reads == 1
    assert builds == {
        "playlist-a": ("persisted-old",),
        "playlist-b": ("persisted-old",),
    }


def test_stale_escalation_reloads_catalogue_for_later_playlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, journal, builds = _runner(monkeypatch, stale_first=True)

    result = runner._run_cycle()

    assert result.ok is True
    assert result.playlists[0].stale_escalation is True
    assert journal.full_reads == 2
    assert builds["playlist-a"] == ("persisted-old",)
    assert builds["playlist-b"] == ("persisted-refreshed",)


def test_failed_reconciliation_reloads_catalogue_for_later_playlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, journal, builds = _runner(
        monkeypatch,
        stale_first=False,
        failed_first=True,
    )

    result = runner._run_cycle()

    assert result.ok is False
    assert result.playlists[0].ok is False
    assert result.playlists[0].stale_escalation is False
    assert journal.full_reads == 2
    assert builds["playlist-a"] == ("persisted-old",)
    assert builds["playlist-b"] == ("persisted-refreshed",)


@pytest.mark.parametrize("verification_kind", ("exact", "degraded"))
def test_failed_stale_retry_reloads_committed_catalogue_before_later_playlist(
    tmp_path: Path,
    verification_kind: str,
) -> None:
    source = next(source for source in BUILTIN_CATALOG.sources if source.id == "abc")
    show_id = spotify_show_id(source)
    new_uri = "spotify:episode:" + "F" * 22
    updated_uris = (new_uri, EPISODE_URI)
    playlist_a = PlaylistDefinition(
        id=PlaylistId("playlist-a"),
        display_name="Playlist A",
        description="test",
        countries=source.countries,
        languages=source.languages,
        enabled=True,
        source_selection=SourceSelection((source.id,)),
        destination=DestinationReference(AdapterId("spotify"), "destination-a"),
    )
    playlist_b = replace(
        playlist_a,
        id=PlaylistId("playlist-b"),
        destination=DestinationReference(AdapterId("spotify"), "destination-b"),
    )
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    detector_journal = PublicDetectorJournal(store)
    detector_journal.initialize()
    seen_at = NOW - timedelta(minutes=5)
    journal.replace_source_episodes(
        source.id,
        (
            SpotifyFirstEpisode(
                source_id=source.id,
                spotify_show_id=show_id,
                spotify_episode_id="E" * 22,
                spotify_episode_uri=EPISODE_URI,
                title="Bulletin",
                edition_at=None,
                timestamp_source=TimestampSource.FIRST_SEEN,
                release_date_raw=NOW.date().isoformat(),
                release_date_precision="day",
                duration_seconds=300,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
            ),
        ),
        coverage_proven=True,
    )
    journal.record_refresh(
        source,
        refreshed_at=NOW,
        reason=RefreshReason.BOOTSTRAP,
        detector_episode_id=None,
        api_episode_id="E" * 22,
        request_count=1,
        complete=True,
        coverage_proven=True,
        coverage_cutoff_at=NOW - timedelta(hours=playlist_a.retention_hours),
    )
    old_fingerprint = desired_fingerprint((EPISODE_URI,))
    store.set_playlist_attestation(
        playlist_b.id,
        destination_id=playlist_b.destination.external_id,
        snapshot_id="snapshot-old",
        desired_fingerprint=old_fingerprint,
        updated_at=NOW,
    )
    record_verification = (
        journal.record_destination_verification
        if verification_kind == "exact"
        else journal.record_degraded_destination_verification
    )
    record_verification(
        playlist_b.id,
        destination_id=playlist_b.destination.external_id,
        desired_fingerprint=old_fingerprint,
        verified_at=NOW,
    )

    class Client:
        def __init__(self) -> None:
            self.catalogue_reads = 0
            self.writes: list[tuple[str, tuple[str, ...]]] = []
            self.destination_b = (EPISODE_URI,)

        def show_episodes(
            self, requested_show_id: str, *, limit: int = 50, offset: int = 0
        ) -> dict[str, Any]:
            assert requested_show_id == show_id
            self.catalogue_reads += 1
            # Keep the old URI: the refresh commits, but stale retry cannot prove removal.
            return catalogue_page(
                [
                    {
                        "id": letter * 22,
                        "uri": "spotify:episode:" + letter * 22,
                        "name": "Bulletin",
                        "release_date": NOW.date().isoformat(),
                        "release_date_precision": "day",
                        "duration_ms": 300_000,
                    }
                    for letter in ("F", "E")
                ],
                limit=limit,
                offset=offset,
            )

        def playlist_items(
            self, destination_id: str, *, limit: int = 50, offset: int = 0
        ) -> dict[str, Any]:
            uris = self.destination_b if destination_id == "destination-b" else ()
            return {
                "items": [{"item": {"type": "episode", "uri": uri}} for uri in uris],
                "next": None,
                "total": len(uris),
            }

        def playlist_snapshot(self, destination_id: str) -> dict[str, Any]:
            del destination_id
            return {
                "snapshot_id": (
                    "snapshot-new" if self.destination_b == updated_uris else "snapshot-old"
                )
            }

        def replace_playlist_items(self, destination_id: str, uris: list[str]) -> dict[str, Any]:
            self.writes.append((destination_id, tuple(uris)))
            if destination_id == "destination-a":
                raise SpotifyApiError(400, "deterministic test rejection")
            self.destination_b = tuple(uris)
            return {"snapshot_id": "snapshot-new"}

    client = Client()
    detector = SimpleNamespace(
        observe_sources=lambda *args, **kwargs: PublicDetectorCycleSummary(1, 0, 1, 0)
    )
    runner = EngineRunner(
        EngineConfig(sources=(source,), playlists=(playlist_a, playlist_b)),
        store,
        _Auth(),  # type: ignore[arg-type]
        detector,
        detector_journal,
        journal,
        client_factory=lambda _token: client,
        clock=lambda: NOW,
    )

    # Both persisted forms really authorize a skip for the old desired before A runs.
    assert (
        runner._destination_skip_evidence(playlist_b, old_fingerprint, now=NOW) == verification_kind
    )

    result = runner.run_cycle()

    assert result.ok is False
    outcome_a, outcome_b = result.playlists
    assert outcome_a.ok is False
    assert outcome_a.stale_escalation is False
    assert outcome_a.error is not None and "http_status=400" in outcome_a.error
    assert result.sources[0].refreshed is False
    assert client.catalogue_reads == 1
    # Reopen SQLite to prove the new episode was committed, not merely held in memory.
    persisted = SpotifyFirstJournal(SQLiteStore(store.path)).list_episodes()
    assert {episode.spotify_episode_uri for episode in persisted} == set(updated_uris)
    state = journal.get_source_state(source.id)
    assert state is not None and state.reason is RefreshReason.STALE_ESCALATION
    # This fingerprint is built before skip evidence is checked, so prewrite rebuilding
    # cannot mask a stale cycle snapshot. The old evidence must not skip the new desired.
    assert outcome_b.desired_fingerprint == desired_fingerprint(updated_uris)
    assert outcome_b.ok is True
    assert outcome_b.local_skip is False
    assert outcome_b.wrote is True
    assert client.writes == [
        ("destination-a", (EPISODE_URI,)),
        ("destination-b", updated_uris),
    ]
