"""Resolve installation-owned managed state into the effective runtime configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from news_bulletin_playlist.managed_state import (\nfrom news_bulletin_playlist.models import EngineConfig
    MANAGED_STATE_FILENAME,
    ManagedStateError,
    compile_engine_config,
    effective_sources,
)
from news_bulletin_playlist.models import EngineConfig
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME
from news_bulletin_playlist.models import EngineConfig

_OBSOLETE_CONFIG_FILENAME = "news-bulletin-playlist.yaml"
_OBSOLETE_CONFIG_PATH_ENV = "NEWS_PLAYLIST_CONFIG"


def load_effective_config(
    data_dir: Path,
    environ: Mapping[str, str],
) -> EngineConfig | None:
    """Load the managed installation configuration or report that none is active."""
    _reject_obsolete_configuration(data_dir, environ)

    managed_path = data_dir / MANAGED_STATE_FILENAME
    if not _path_present(managed_path) and not _path_present(
        data_dir / V11_OPERATIONAL_FILENAME
    ):
        return None

    try:
        store = open_managed_state_store(data_dir)
        state = store.load()
        disabled = {
            source.id for source in effective_sources(store.catalog, state) if not source.enabled
        }
        # Before dormant membership, an active playlist with only disabled assignments was
        # valid and preserved Spotify. Pause it durably so startup and Admin remain available
        # without ever passing an active, empty selection to the engine. Keep every assignment.
        recovered = replace(
            state,
            playlists=tuple(
                replace(playlist, enabled=False)
                if playlist.enabled
                and playlist.source_ids
                and disabled.issuperset(playlist.source_ids)
                else playlist
                for playlist in state.playlists
            ),
        )
        config = compile_engine_config(store.catalog, recovered)
        if recovered != state:
            try:
                store.save(recovered)
            except OSError as exc:
                raise ManagedStateError(
                    "dormant-assignment recovery could not be persisted safely"
                ) from exc
    except ManagedStateError as exc:
        raise RuntimeError(f"invalid managed configuration: {exc}") from exc
    if not any(playlist.enabled for playlist in config.playlists):
        return None
    return config


def _reject_obsolete_configuration(data_dir: Path, environ: Mapping[str, str]) -> None:
    explicit = environ.get(_OBSOLETE_CONFIG_PATH_ENV)
    if explicit is not None and explicit.strip():
        raise RuntimeError(
            "NEWS_PLAYLIST_CONFIG is no longer supported; remove it and manage playlists "
            "through /admin/ with /data/managed-state.json"
        )

    legacy_path = data_dir / _OBSOLETE_CONFIG_FILENAME
    if _path_present(legacy_path):
        raise RuntimeError(
            "legacy /data/news-bulletin-playlist.yaml is no longer supported; "
            "remove or relocate the obsolete file and manage playlists through /admin/"
        )


def _path_present(path: Path) -> bool:
    """Treat symlinks, including broken ones, as present so they cannot hide state."""
    return path.is_symlink() or path.exists()
