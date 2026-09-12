from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from _spotify_catalogue import catalogue_page
from test_engine import (
    NOW,
    SER_TITLE,
    SER_URI,
    _Auth,
    _config,
    _EmptyCatalogueSpotify,
    _FailingCatalogueSpotify,
    _rss,
    _Spotify,
    _store,
)

from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.persistence import PersistenceError


def test_matching_evaluated_count_stays_zero_when_cooldown_blocks_planned_work(
    tmp_path: Path,
) -> None:
    auth = _Auth()
    spotify = _EmptyCatalogueSpotify()
    current = NOW
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    assert runner.run_cycle().ok
    show_calls = spotify.show_calls
    auth_calls = auth.calls
    current = NOW + timedelta(minutes=15)
    runner.rate_limit_journal.activate(observed_at=current, retry_after_seconds=600)

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_work_count == 1
    assert diagnostic.matching_reason_retry_due_count == 1
    assert diagnostic.matching_evaluated_count == 0
    assert diagnostic.catalogue_requested is False
    assert diagnostic.catalogue_inspected is False
    assert diagnostic.catalogue_request_count == 0
    assert spotify.show_calls == show_calls
    assert auth.calls == auth_calls
    assert result.provider_session_created is False


def test_matching_evaluated_count_stays_zero_when_catalogue_fetch_fails(
    tmp_path: Path,
) -> None:
    spotify = _FailingCatalogueSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_work_count == 1
    assert diagnostic.matching_reason_new_match_count == 1
    assert diagnostic.matching_evaluated_count == 0
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is False
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == 1
    assert result.sources[0].matching_ok is False


def test_matching_evaluated_count_counts_fresh_matched_edition_once(tmp_path: Path) -> None:
    spotify = _Spotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert result.ok
    assert diagnostic.matching_work_count == 1
    assert diagnostic.matching_evaluated_count == 1
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is True
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == 1


def test_matching_evaluated_count_survives_match_persistence_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    spotify = _EmptyCatalogueSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    def fail_match_persistence(*_args, **_kwargs) -> None:
        raise PersistenceError("synthetic match persistence failure")

    monkeypatch.setattr(runner.store, "set_match_state", fail_match_persistence)

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert not result.ok
    assert result.error == "synthetic match persistence failure"
    assert diagnostic.matching_work_count == 1
    assert diagnostic.matching_evaluated_count == 1
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is True
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == 1


def test_matching_evaluation_observability_failure_does_not_change_engine_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    spotify = _EmptyCatalogueSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    def fail_observability(*_args, **_kwargs) -> None:
        raise RuntimeError("synthetic observability failure")

    monkeypatch.setattr(runner, "_record_matching_evaluation", fail_observability)

    result = runner.run_cycle()

    assert result.ok
    assert result.sources[0].matching_ok is True
    assert result.sources[0].matching_diagnostics.matching_evaluated_count == 0
    assert spotify.show_calls == 1


def test_prewrite_reevaluation_counts_actual_work_separately_from_planned_work(
    tmp_path: Path,
) -> None:
    class MissingMappedEpisodeSpotify(_Spotify):
        missing = False

        def show_episodes(
            self,
            show_id: str,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> dict[str, Any]:
            if not self.missing:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            self.show_calls += 1
            return catalogue_page(
                [
                    {
                        "uri": "spotify:episode:" + "2" * 22,
                        "name": "unrelated",
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    }
                ],
                offset=offset,
                limit=limit,
            )

    spotify = MissingMappedEpisodeSpotify()
    current = NOW
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
        local_skips_enabled=False,
    )
    assert runner.run_cycle().ok
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    initial_show_calls = spotify.show_calls

    current = NOW + timedelta(minutes=10)
    spotify.missing = True
    spotify.items["destination-first"] = []
    runner.planner_journal.begin_destination_verification("first")

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert result.ok
    assert diagnostic.matching_work_count == 0
    assert diagnostic.matching_evaluated_count == 1
    assert diagnostic.catalogue_audit_due is False
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is True
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == initial_show_calls + 1
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") is None
    assert spotify.replacements == [("destination-first", (SER_URI,))]
    assert SER_TITLE not in repr(diagnostic)
