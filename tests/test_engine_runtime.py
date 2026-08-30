from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.engine import (
    EngineCycleResult,
    OperationalStatus,
    PlaylistCycleOutcome,
    SourceCycleOutcome,
)
from news_bulletin_playlist.engine_runtime import (
    DEFAULT_CONFIG_FILENAME,
    AuthSynchronization,
    _load_runtime_config,
    _operational_status_page,
    _runtime_interval,
    serve,
)
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.spotify.auth import AuthorizationState

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)


def _valid_config() -> str:
    return """schema_version: 1
sources:
  - id: ser
    display_name: Cadena SER
    countries: [ES]
    languages: [es]
    timezone: Europe/Madrid
    enabled: true
    parser_id: ser
    endpoint_url: https://example.test/ser.xml
    external_references:
      - system: spotify
        resource_type: show
        external_id: ser-show
playlists:
  - id: spanish
    display_name: Spanish News
    description: test
    countries: [ES]
    languages: [es]
    enabled: true
    source_selection:
      explicit: [ser]
    destination:
      adapter_id: spotify
      external_id: playlist-id
"""


def test_runtime_interval_defaults_to_ten_minutes() -> None:
    assert _runtime_interval({}) == timedelta(minutes=10)
    assert _runtime_interval({"NEWS_PLAYLIST_INTERVAL_SECONDS": "600"}) == timedelta(minutes=10)


def test_runtime_interval_rejects_tight_retry_values() -> None:
    with pytest.raises(RuntimeError, match="at least 60 seconds"):
        _runtime_interval({"NEWS_PLAYLIST_INTERVAL_SECONDS": "59"})
    with pytest.raises(RuntimeError, match="must be an integer"):
        _runtime_interval({"NEWS_PLAYLIST_INTERVAL_SECONDS": "fast"})


def test_runtime_config_is_optional_until_production_file_exists(tmp_path: Path) -> None:
    assert _load_runtime_config(tmp_path, {}) is None
    assert _load_runtime_config(tmp_path, {"NEWS_PLAYLIST_CONFIG": "  "}) is None


def test_runtime_config_loads_default_data_file(tmp_path: Path) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_FILENAME
    config_path.write_text(_valid_config(), encoding="utf-8")

    config = _load_runtime_config(tmp_path, {})

    assert config is not None
    assert [source.id for source in config.sources] == [SourceId("ser")]
    assert [playlist.id for playlist in config.playlists] == [PlaylistId("spanish")]


def test_explicit_missing_runtime_config_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    with pytest.raises(RuntimeError, match="does not exist"):
        _load_runtime_config(tmp_path, {"NEWS_PLAYLIST_CONFIG": str(missing)})


def test_configured_engine_requires_production_spotify_auth(tmp_path: Path) -> None:
    (tmp_path / DEFAULT_CONFIG_FILENAME).write_text(_valid_config(), encoding="utf-8")

    with pytest.raises(RuntimeError, match="requires production Spotify"):
        serve(data_dir=tmp_path, stop_event=threading.Event(), environ={})


def test_operational_status_page_reports_cycle_without_secret_material() -> None:
    status = OperationalStatus(configured=True)
    cycle = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=12),
        ok=False,
        sources=(
            SourceCycleOutcome(
                source_id=SourceId("ser"),
                collection_ok=False,
                matching_ok=True,
                edition_count=0,
                matched_count=1,
                last_success_at=NOW - timedelta(minutes=10),
                error="source unavailable",
            ),
        ),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spanish"),
                ok=True,
                desired_count=1,
                applied_count=1,
                wrote=False,
                last_success_at=NOW,
            ),
        ),
        error="cycle completed with 1 source failure(s) and 0 playlist failure(s)",
    )
    status.finish_cycle(cycle, next_run_at=NOW + timedelta(minutes=10))

    body = _operational_status_page(
        ready=True,
        spotify_state=AuthorizationState.CONNECTED,
        status=status,
    ).decode("utf-8")

    assert "Connected" in body
    assert "2026-08-30T10:10:00Z" in body
    assert "source unavailable" in body
    assert "0 fetched / 1 matched" in body
    assert "1 desired / 1 verified" in body
    assert "unchanged" in body
    for secret in ("access-token-sentinel", "refresh-token-sentinel", "authorization-code"):
        assert secret not in body


def test_auth_synchronization_serializes_competing_operations() -> None:
    synchronization = AuthSynchronization()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with synchronization.hold():
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second() -> None:
        assert first_entered.wait(timeout=2)
        with synchronization.hold():
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(timeout=2)
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    assert second_entered.wait(timeout=2)
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
