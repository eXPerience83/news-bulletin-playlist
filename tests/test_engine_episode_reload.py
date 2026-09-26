from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import news_bulletin_playlist.engine as engine_module
from news_bulletin_playlist.engine import (
    EngineRunner,
    PlaylistCycleOutcome,
    SourceCycleOutcome,
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


def _playlist_outcome(playlist_id: str, *, stale_escalation: bool) -> PlaylistCycleOutcome:
    return PlaylistCycleOutcome(
        playlist_id=playlist_id,  # type: ignore[arg-type]
        ok=True,
        desired_count=1,
        applied_count=1,
        wrote=False,
        last_success_at=NOW,
        stale_escalation=stale_escalation,
    )


def _runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stale_first: bool,
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
    runner.config = SimpleNamespace(sources=(source,), playlists=playlists)  # type: ignore[assignment]
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
        stale_escalation = stale_first and playlist.id == "playlist-a"
        if stale_escalation:
            journal.episodes = ("persisted-refreshed",)
        return _playlist_outcome(playlist.id, stale_escalation=stale_escalation)

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
