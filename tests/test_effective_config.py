from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.effective_config import load_effective_config
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedState,
    ManagedStateStore,
    activate_template,
)
from news_bulletin_playlist.models import PlaylistId, SourceId


def _managed_state(tmp_path: Path, *, enabled: bool = True) -> ManagedState:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    playlist = activate_template(template, "managed-destination")
    if not enabled:
        playlist = replace(playlist, enabled=False)
    state = ManagedState(playlists=(playlist,))
    ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME).save(state)
    return state


def test_missing_configuration_returns_none(tmp_path: Path) -> None:
    assert load_effective_config(tmp_path, {}) is None
    assert load_effective_config(tmp_path, {"NEWS_PLAYLIST_CONFIG": "  "}) is None


def test_managed_state_is_only_configuration_source(tmp_path: Path) -> None:
    _managed_state(tmp_path)

    config = load_effective_config(tmp_path, {})

    assert config is not None
    assert [playlist.id for playlist in config.playlists] == [PlaylistId("spain_spanish_news")]
    assert config.playlists[0].display_name == "Noticias en Español"
    assert tuple(map(str, config.playlists[0].languages)) == ("es-ES",)
    assert config.playlists[0].source_selection.explicit == (
        SourceId("ser"),
        SourceId("rne"),
        SourceId("ondacero"),
        SourceId("abc"),
    )


def test_broken_managed_state_symlink_fails_closed(tmp_path: Path) -> None:
    managed_path = tmp_path / MANAGED_STATE_FILENAME
    managed_path.symlink_to(tmp_path / "missing-state.json")

    with pytest.raises(RuntimeError, match="invalid managed configuration"):
        load_effective_config(tmp_path, {})


def test_empty_or_all_paused_managed_state_has_no_active_engine(tmp_path: Path) -> None:
    ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME).save(ManagedState())
    assert load_effective_config(tmp_path, {}) is None

    _managed_state(tmp_path, enabled=False)
    assert load_effective_config(tmp_path, {}) is None


def test_obsolete_explicit_yaml_setting_fails_with_migration_message(tmp_path: Path) -> None:
    _managed_state(tmp_path)

    with pytest.raises(RuntimeError, match="NEWS_PLAYLIST_CONFIG is no longer supported"):
        load_effective_config(
            tmp_path,
            {"NEWS_PLAYLIST_CONFIG": str(tmp_path / "old-config.yaml")},
        )


def test_obsolete_default_yaml_file_fails_even_with_managed_state(tmp_path: Path) -> None:
    _managed_state(tmp_path)
    (tmp_path / "news-bulletin-playlist.yaml").write_text("obsolete", encoding="utf-8")

    with pytest.raises(RuntimeError, match="legacy /data/news-bulletin-playlist.yaml"):
        load_effective_config(tmp_path, {})


def test_broken_obsolete_yaml_symlink_also_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "news-bulletin-playlist.yaml").symlink_to(tmp_path / "missing.yaml")

    with pytest.raises(RuntimeError, match="legacy /data/news-bulletin-playlist.yaml"):
        load_effective_config(tmp_path, {})


def test_invalid_managed_state_fails_closed(tmp_path: Path) -> None:
    (tmp_path / MANAGED_STATE_FILENAME).write_text(
        '{"schema_version":999,"playlists":[]}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="invalid managed configuration"):
        load_effective_config(tmp_path, {})


def test_managed_state_changes_are_observed_on_each_load(tmp_path: Path) -> None:
    state = _managed_state(tmp_path)
    first = load_effective_config(tmp_path, {})
    assert first is not None
    assert first.playlists[0].enabled is True

    paused = replace(state.playlists[0], enabled=False)
    ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME).save(ManagedState(playlists=(paused,)))

    assert load_effective_config(tmp_path, {}) is None
