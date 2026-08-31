"""Resolve installation-owned managed state or the legacy full-YAML configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.config import ConfigError, load_config
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedStateError,
    ManagedStateStore,
    compile_engine_config,
)
from news_bulletin_playlist.models import EngineConfig

DEFAULT_CONFIG_FILENAME = "news-bulletin-playlist.yaml"
CONFIG_PATH_ENV = "NEWS_PLAYLIST_CONFIG"


def load_effective_config(
    data_dir: Path,
    environ: Mapping[str, str],
) -> EngineConfig | None:
    """Load one unambiguous effective configuration without changing the P1 engine model."""
    raw_explicit = environ.get(CONFIG_PATH_ENV)
    explicit = raw_explicit.strip() if raw_explicit is not None and raw_explicit.strip() else None
    if explicit is not None:
        return _load_legacy(Path(explicit), explicit=True)

    managed_path = data_dir / MANAGED_STATE_FILENAME
    legacy_path = data_dir / DEFAULT_CONFIG_FILENAME
    managed_present = _path_present(managed_path)
    legacy_present = _path_present(legacy_path)
    if managed_present and legacy_present:
        raise RuntimeError(
            "both managed-state.json and the default legacy news-bulletin-playlist.yaml exist; "
            "remove or explicitly select one configuration source"
        )

    if managed_present:
        try:
            state = ManagedStateStore(managed_path).load()
            config = compile_engine_config(BUILTIN_CATALOG, state)
        except ManagedStateError as exc:
            raise RuntimeError(f"invalid managed configuration: {exc}") from exc
        if not any(playlist.enabled for playlist in config.playlists):
            return None
        return config

    if legacy_present:
        return _load_legacy(legacy_path, explicit=False)
    return None


def _path_present(path: Path) -> bool:
    """Treat symlinks, including broken ones, as present so they cannot hide state."""
    return path.is_symlink() or path.exists()


def _load_legacy(path: Path, *, explicit: bool) -> EngineConfig:
    if not path.exists():
        if explicit:
            raise RuntimeError(f"configured engine YAML does not exist: {path}")
        raise RuntimeError(f"legacy engine YAML no longer exists: {path}")
    try:
        return load_config(path)
    except ConfigError as exc:
        raise RuntimeError(f"invalid engine configuration: {exc}") from exc
