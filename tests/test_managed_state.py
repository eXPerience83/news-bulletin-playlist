from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.models import AdapterId, DestinationReference, SourceId


def test_builtin_catalog_has_stable_first_template_and_supported_sources() -> None:
    assert [str(source.id) for source in BUILTIN_CATALOG.sources] == [
        "ser",
        "rne",
        "ondacero",
        "abc",
        "cnn",
        "rfi_es",
        "bbc_world",
        "rfi_fr",
        "dlf_news",
        "rmf_fakty",
    ]
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    assert template.display_name == "Noticias en Español"
    assert "48h" not in template.display_name
    assert template.cover_id == "spain_spanish_news"
    assert template.default_source_ids == (
        SourceId("ser"),
        SourceId("rne"),
        SourceId("ondacero"),
        SourceId("abc"),
        SourceId("cnn"),
        SourceId("rfi_es"),
    )
    assert template.duration_policy.default_max_seconds == 1800
    assert template.duration_policy.exceptions == ()


def test_catalog_rejects_template_that_references_unknown_source() -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    broken = replace(
        template,
        default_source_ids=template.default_source_ids + (SourceId("missing"),),
    )
    with pytest.raises(ValueError, match="unknown source"):
        BuiltInCatalog(sources=BUILTIN_CATALOG.sources, playlists=(broken,))


def test_catalog_rejects_source_without_exactly_one_spotify_show_reference() -> None:
    broken_source = replace(BUILTIN_CATALOG.sources[0], external_references=())
    with pytest.raises(ValueError, match="exactly one Spotify show reference"):
        BuiltInCatalog(
            sources=(broken_source,) + BUILTIN_CATALOG.sources[1:],
            playlists=BUILTIN_CATALOG.playlists,
        )


def test_activation_snapshots_defaults_instead_of_copying_the_catalog() -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = activate_template(template, "spotify-playlist-id")
    assert managed.display_name == "Noticias en Español"
    assert managed.description == template.description
    assert managed.source_ids == template.default_source_ids
    assert managed.cover_id == template.cover_id
    assert managed.destination == DestinationReference(AdapterId("spotify"), "spotify-playlist-id")
    assert managed.max_duration_seconds == 1800


def test_managed_state_store_round_trip_is_owner_only(tmp_path: Path) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    state = ManagedState(playlists=(activate_template(template, "spotify-playlist-id"),))
    path = tmp_path / MANAGED_STATE_FILENAME
    store = ManagedStateStore(path)

    store.save(state)

    assert store.load() == state
    assert os.stat(path).st_mode & 0o777 == 0o600
    raw = path.read_text(encoding="utf-8")
    assert "Noticias en Español" in raw
    assert '"max_duration_seconds": 1800' in raw
    assert "endpoint_url" not in raw
    assert "spotify_show_id" not in raw


def test_managed_state_store_rejects_broken_symlink_for_load_and_save(tmp_path: Path) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    state = ManagedState(playlists=(activate_template(template, "spotify-playlist-id"),))
    path = tmp_path / MANAGED_STATE_FILENAME
    missing_target = tmp_path / "missing-state.json"
    path.symlink_to(missing_target)
    store = ManagedStateStore(path)

    with pytest.raises(ManagedStateError, match="not a regular file"):
        store.load()
    with pytest.raises(ManagedStateError, match="not a regular file"):
        store.save(state)

    assert path.is_symlink()
    assert not missing_target.exists()


def test_managed_state_repeated_save_is_idempotent(tmp_path: Path) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    state = ManagedState(playlists=(activate_template(template, "spotify-playlist-id"),))
    path = tmp_path / MANAGED_STATE_FILENAME
    store = ManagedStateStore(path)

    store.save(state)
    first_document = path.read_bytes()
    store.save(state)

    assert path.read_bytes() == first_document
    assert store.load() == state


@pytest.mark.parametrize("field", ["retention_hours", "max_episodes", "max_duration_seconds"])
@pytest.mark.parametrize("value", [0, -1])
def test_managed_state_store_rejects_non_positive_policy_before_write(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = activate_template(template, "spotify-playlist-id")
    invalid = replace(managed, **{field: value})
    path = tmp_path / MANAGED_STATE_FILENAME
    store = ManagedStateStore(path)

    with pytest.raises(ManagedStateError, match="must be positive"):
        store.save(ManagedState(playlists=(invalid,)))

    assert not path.exists()


def test_legacy_managed_state_without_duration_field_loads_and_inherits_template_default(
    tmp_path: Path,
) -> None:
    path = tmp_path / MANAGED_STATE_FILENAME
    path.write_text(
        """{
  "schema_version": 1,
  "playlists": [{
    "id": "spain_spanish_news",
    "template_id": "spain_spanish_news",
    "enabled": true,
    "display_name": "Noticias España",
    "description": "legacy",
    "cover_id": "spain_spanish_news",
    "source_ids": ["ser"],
    "destination": {"adapter_id": "spotify", "external_id": "legacy-destination"},
    "retention_hours": 48,
    "max_episodes": 100
  }]
}
""",
        encoding="utf-8",
    )

    state = ManagedStateStore(path).load()
    assert state.playlists[0].max_duration_seconds is None
    compiled = compile_engine_config(BUILTIN_CATALOG, state)
    assert compiled.playlists[0].duration_policy.default_max_seconds == 1800
