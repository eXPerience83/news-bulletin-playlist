from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import (
    BUILTIN_CATALOG,
    BuiltInCatalog,
    PlaylistTemplate,
)
from news_bulletin_playlist.collection import required_sources
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedPlaylist,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.models import (
    AdapterId,
    CountryCode,
    DestinationReference,
    LanguageTag,
    PlaylistId,
    SourceId,
)


def test_builtin_catalog_has_stable_first_template_and_supported_sources() -> None:
    assert [str(source.id) for source in BUILTIN_CATALOG.sources] == [
        "ser",
        "rne",
        "ondacero",
        "cnn",
    ]
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    assert template.display_name == "Noticias España"
    assert "48h" not in template.display_name
    assert template.cover_id == "spain_spanish_news"
    assert template.default_source_ids == (
        SourceId("ser"),
        SourceId("rne"),
        SourceId("ondacero"),
        SourceId("cnn"),
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
    assert managed.display_name == "Noticias España"
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
    assert "Noticias España" in raw
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


def test_explicit_managed_duration_override_wins_over_template_default() -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = replace(
        activate_template(template, "spotify-playlist-id"),
        max_duration_seconds=600,
    )
    compiled = compile_engine_config(BUILTIN_CATALOG, ManagedState(playlists=(managed,)))
    assert compiled.playlists[0].duration_policy.default_max_seconds == 600


def test_managed_state_store_rejects_duplicate_sources_before_write(tmp_path: Path) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = activate_template(template, "spotify-playlist-id")
    duplicate = replace(managed, source_ids=(SourceId("ser"), SourceId("ser")))
    path = tmp_path / MANAGED_STATE_FILENAME
    store = ManagedStateStore(path)

    with pytest.raises(ManagedStateError, match="source_ids contains duplicates"):
        store.save(ManagedState(playlists=(duplicate,)))

    assert not path.exists()


def test_managed_state_store_rejects_enabled_playlist_without_sources_before_write(
    tmp_path: Path,
) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = activate_template(template, "spotify-playlist-id")
    invalid = replace(managed, source_ids=())
    path = tmp_path / MANAGED_STATE_FILENAME
    store = ManagedStateStore(path)

    with pytest.raises(ManagedStateError, match="must select a source"):
        store.save(ManagedState(playlists=(invalid,)))

    assert not path.exists()


def test_managed_state_store_rejects_duplicate_enabled_destination_before_write(
    tmp_path: Path,
) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    first = activate_template(template, "spotify-playlist-id")
    second = replace(first, id=PlaylistId("second-playlist"))
    path = tmp_path / MANAGED_STATE_FILENAME
    store = ManagedStateStore(path)

    with pytest.raises(ManagedStateError, match="duplicate enabled destination"):
        store.save(ManagedState(playlists=(first, second)))

    assert not path.exists()


def test_managed_state_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / MANAGED_STATE_FILENAME
    path.write_text('{"schema_version":1,"schema_version":1,"playlists":[]}', encoding="utf-8")
    with pytest.raises(ManagedStateError, match="duplicate JSON key"):
        ManagedStateStore(path).load()


def test_catalog_additions_do_not_change_existing_source_selection() -> None:
    original_template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = activate_template(original_template, "spotify-playlist-id")
    future_source = replace(
        BUILTIN_CATALOG.sources[0],
        id=SourceId("future"),
        display_name="Future News",
        endpoint_url="https://example.test/future.xml",
    )
    expanded_template = replace(
        original_template,
        default_source_ids=original_template.default_source_ids + (future_source.id,),
    )
    expanded_catalog = BuiltInCatalog(
        sources=BUILTIN_CATALOG.sources + (future_source,),
        playlists=(expanded_template,),
    )

    config = compile_engine_config(expanded_catalog, ManagedState(playlists=(managed,)))

    assert config.playlists[0].source_selection.explicit == original_template.default_source_ids
    assert future_source.id not in config.playlists[0].source_selection.explicit


def test_many_playlists_can_share_one_source_without_duplicate_collection() -> None:
    first_template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    second_template = PlaylistTemplate(
        id=PlaylistId("world_spanish_news"),
        display_name="Noticias Mundo",
        description="Actualidad internacional en español.",
        countries=(CountryCode("US"),),
        languages=(LanguageTag("es"),),
        default_source_ids=(SourceId("cnn"), SourceId("ser")),
        cover_id="international_spanish_news",
    )
    catalog = BuiltInCatalog(
        sources=BUILTIN_CATALOG.sources,
        playlists=(first_template, second_template),
    )
    first = activate_template(first_template, "first-destination")
    second = activate_template(second_template, "second-destination")

    config = compile_engine_config(catalog, ManagedState(playlists=(first, second)))
    sources = required_sources(config)

    assert [str(source.id) for source in sources] == ["ser", "rne", "ondacero", "cnn"]
    assert sum(source.id == SourceId("ser") for source in sources) == 1
    assert sum(source.id == SourceId("cnn") for source in sources) == 1


def test_compiler_fails_closed_for_unknown_catalog_references() -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = activate_template(template, "spotify-playlist-id")
    broken = ManagedPlaylist(
        id=managed.id,
        template_id=managed.template_id,
        enabled=True,
        display_name=managed.display_name,
        description=managed.description,
        cover_id=managed.cover_id,
        source_ids=(SourceId("missing-source"),),
        destination=managed.destination,
        retention_hours=managed.retention_hours,
        max_episodes=managed.max_episodes,
        max_duration_seconds=managed.max_duration_seconds,
    )
    with pytest.raises(ManagedStateError, match="unknown source"):
        compile_engine_config(BUILTIN_CATALOG, ManagedState(playlists=(broken,)))


def test_disabled_playlist_may_keep_zero_selected_sources() -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = activate_template(template, "spotify-playlist-id")
    paused = ManagedPlaylist(
        id=managed.id,
        template_id=managed.template_id,
        enabled=False,
        display_name=managed.display_name,
        description=managed.description,
        cover_id=managed.cover_id,
        source_ids=(),
        destination=managed.destination,
        retention_hours=managed.retention_hours,
        max_episodes=managed.max_episodes,
        max_duration_seconds=managed.max_duration_seconds,
    )
    config = compile_engine_config(BUILTIN_CATALOG, ManagedState(playlists=(paused,)))
    assert config.playlists[0].enabled is False
    assert required_sources(config) == ()
