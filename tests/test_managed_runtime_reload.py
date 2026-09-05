from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine_runtime import (
    ConfigurationSynchronization,
    ReloadingEngineCycleRunner,
    _load_runtime_config,
    _playlist_write_contract,
)
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedState,
    ManagedStateStore,
    activate_template,
)
from news_bulletin_playlist.models import (
    CountryCode,
    ExternalReference,
    LanguageTag,
    ParserId,
    SourceId,
)
from news_bulletin_playlist.persistence import SQLiteStore


class _NeverAuth:
    def __init__(self) -> None:
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        del now
        self.calls += 1
        raise AssertionError("authorization must not be reached after all playlists are paused")


def test_reloading_runner_observes_managed_state_change_before_next_cycle(tmp_path: Path) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    active = activate_template(template, "managed-destination")
    state_store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)
    state_store.save(ManagedState(playlists=(active,)))
    assert _load_runtime_config(tmp_path, {}) is not None

    sqlite = SQLiteStore(tmp_path / "engine.sqlite3")
    sqlite.initialize()
    auth = _NeverAuth()
    runner = ReloadingEngineCycleRunner(tmp_path, {}, sqlite, auth)

    paused = replace(active, enabled=False)
    state_store.save(ManagedState(playlists=(paused,)))
    result = runner.run_cycle()

    assert not result.ok
    assert result.error == "production engine configuration is no longer available"
    assert auth.calls == 0


NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
SER_TITLE = "Las noticias de la SER, 11:00 (30/08/2026)"
SER_URI = "spotify:episode:ser-1100"


class _Auth:
    def get_access_token(self, *, now: datetime | None = None) -> str:
        assert now is not None
        return "access-token"


class _Spotify:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.replacements: list[tuple[str, tuple[str, ...]]] = []

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "4EwwdoHHYmbt49UXODQMpi"
        assert offset == 0
        return {
            "items": [
                {
                    "uri": SER_URI,
                    "name": SER_TITLE,
                    "release_date": "2026-08-30",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
            ],
            "next": None,
        }

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        selected = self.items[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in selected],
            "next": None,
            "total": len(self.items),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        self.items = list(uris)
        self.replacements.append((playlist_id, tuple(uris)))
        return {"snapshot_id": "snapshot-write"}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        return {"snapshot_id": "snapshot-write"}


def _active_ser_state(tmp_path: Path):  # type: ignore[no-untyped-def]
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    active = replace(
        activate_template(template, "managed-destination"),
        source_ids=(SourceId("ser"),),
    )
    state_store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)
    state_store.save(ManagedState(playlists=(active,)))
    sqlite = SQLiteStore(tmp_path / "engine.sqlite3")
    sqlite.initialize()
    return active, state_store, sqlite


def _ser_rss() -> bytes:
    return (
        "<rss><channel><item>"
        "<guid>ser-1</guid>"
        f"<title>{SER_TITLE}</title>"
        "<pubDate>Sun, 30 Aug 2026 09:05:00 +0000</pubDate>"
        "</item></channel></rss>"
    ).encode()


def test_reloading_runner_does_not_hold_configuration_lock_during_feed_io(
    tmp_path: Path,
) -> None:
    _, state_store, sqlite = _active_ser_state(tmp_path)
    synchronization = ConfigurationSynchronization()
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    admin_acquired = threading.Event()
    spotify = _Spotify()
    results: list[object] = []

    def fetcher(_url: str) -> bytes:
        fetch_started.set()
        assert release_fetch.wait(timeout=2)
        return _ser_rss()

    runner = ReloadingEngineCycleRunner(
        tmp_path,
        {},
        sqlite,
        _Auth(),
        synchronization,
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )
    cycle = threading.Thread(target=lambda: results.append(runner.run_cycle()))
    cycle.start()
    assert fetch_started.wait(timeout=2)

    def no_op_admin_save() -> None:
        with synchronization.hold():
            state_store.save(state_store.load())
            admin_acquired.set()

    admin = threading.Thread(target=no_op_admin_save)
    admin.start()
    assert admin_acquired.wait(timeout=0.5)
    admin.join(timeout=1)
    assert not admin.is_alive()

    release_fetch.set()
    cycle.join(timeout=2)
    assert not cycle.is_alive()
    assert len(results) == 1
    result = results[0]
    assert result.ok is True
    assert spotify.replacements == [("managed-destination", (SER_URI,))]


def test_reloading_runner_prevents_stale_write_after_playlist_is_paused(
    tmp_path: Path,
) -> None:
    active, state_store, sqlite = _active_ser_state(tmp_path)
    synchronization = ConfigurationSynchronization()
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    spotify = _Spotify()
    results: list[object] = []

    def fetcher(_url: str) -> bytes:
        fetch_started.set()
        assert release_fetch.wait(timeout=2)
        return _ser_rss()

    runner = ReloadingEngineCycleRunner(
        tmp_path,
        {},
        sqlite,
        _Auth(),
        synchronization,
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )
    cycle = threading.Thread(target=lambda: results.append(runner.run_cycle()))
    cycle.start()
    assert fetch_started.wait(timeout=2)

    with synchronization.hold():
        state_store.save(ManagedState(playlists=(replace(active, enabled=False),)))

    release_fetch.set()
    cycle.join(timeout=2)
    assert not cycle.is_alive()
    assert len(results) == 1
    result = results[0]
    assert result.ok is False
    playlists = result.playlists
    assert len(playlists) == 1
    assert playlists[0].wrote is None
    assert "configuration changed during cycle" in (playlists[0].error or "")
    assert spotify.replacements == []


def test_item_write_contract_ignores_presentation_and_tracks_item_inputs(tmp_path: Path) -> None:
    active, _, _ = _active_ser_state(tmp_path)
    config = _load_runtime_config(tmp_path, {})
    assert config is not None
    baseline = _playlist_write_contract(config, active.id)
    assert baseline is not None
    playlist = config.playlists[0]
    source = config.sources[0]

    cosmetic_playlist = replace(playlist, display_name="Renamed", description="Changed")
    cosmetic_source = replace(
        source,
        display_name="Presentation only",
        countries=(CountryCode("US"),),
        languages=(LanguageTag("en"),),
    )
    cosmetic = replace(config, playlists=(cosmetic_playlist,), sources=(cosmetic_source,))
    assert _playlist_write_contract(cosmetic, active.id) == baseline

    changed_playlists = (
        replace(playlist, destination=replace(playlist.destination, external_id="other")),
        replace(playlist, source_selection=replace(playlist.source_selection, explicit=())),
        replace(playlist, retention_hours=1),
        replace(playlist, max_episodes=1),
        replace(playlist, ordering=playlist.ordering.__class__.PUBLISHED_AT_DESC),
        replace(
            playlist, duration_policy=replace(playlist.duration_policy, default_max_seconds=60)
        ),
        replace(playlist, enabled=False),
    )
    for changed in changed_playlists:
        assert (
            _playlist_write_contract(replace(config, playlists=(changed,)), active.id) != baseline
        )

    changed_sources = (
        replace(source, enabled=False),
        replace(source, timezone=source.timezone.__class__("UTC")),
        replace(source, parser_id=ParserId("cnn")),
        replace(source, endpoint_url="https://example.test/changed.xml"),
        replace(
            source,
            external_references=(ExternalReference("spotify", "show", "other-show"),),
        ),
        replace(source, spotify_release_delay_days=1),
    )
    for changed in changed_sources:
        assert _playlist_write_contract(replace(config, sources=(changed,)), active.id) != baseline
