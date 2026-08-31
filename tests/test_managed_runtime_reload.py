from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine_runtime import ReloadingEngineCycleRunner, _load_runtime_config
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedState,
    ManagedStateStore,
    activate_template,
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
