from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.effective_config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_FILENAME,
    load_effective_config,
)
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedState,
    ManagedStateStore,
    activate_template,
)
from news_bulletin_playlist.models import PlaylistId, SourceId


def _legacy_yaml(playlist_id: str = "legacy") -> str:
    return f"""schema_version: 1
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
  - id: {playlist_id}
    display_name: Legacy News
    description: test
    countries: [ES]
    languages: [es]
    enabled: true
    source_selection:
      explicit: [ser]
    destination:
      adapter_id: spotify
      external_id: legacy-destination
"""


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


def test_managed_state_is_normal_configuration_source(tmp_path: Path) -> None:
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


def test_default_legacy_yaml_remains_compatible_when_no_managed_state(tmp_path: Path) -> None:
    (tmp_path / DEFAULT_CONFIG_FILENAME).write_text(_legacy_yaml(), encoding="utf-8")

    config = load_effective_config(tmp_path, {})

    assert config is not None
    assert [playlist.id for playlist in config.playlists] == [PlaylistId("legacy")]


def test_managed_and_default_legacy_sources_fail_closed_as_ambiguous(tmp_path: Path) -> None:
    _managed_state(tmp_path)
    (tmp_path / DEFAULT_CONFIG_FILENAME).write_text(_legacy_yaml(), encoding="utf-8")

    with pytest.raises(RuntimeError, match="both managed-state.json"):
        load_effective_config(tmp_path, {})


def test_explicit_legacy_override_remains_authoritative(tmp_path: Path) -> None:
    _managed_state(tmp_path)
    explicit = tmp_path / "advanced.yaml"
    explicit.write_text(_legacy_yaml("advanced"), encoding="utf-8")

    config = load_effective_config(tmp_path, {CONFIG_PATH_ENV: str(explicit)})

    assert config is not None
    assert [playlist.id for playlist in config.playlists] == [PlaylistId("advanced")]


def test_explicit_missing_legacy_override_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="does not exist"):
        load_effective_config(tmp_path, {CONFIG_PATH_ENV: str(tmp_path / "missing.yaml")})


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
