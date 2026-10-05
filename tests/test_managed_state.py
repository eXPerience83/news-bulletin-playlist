from __future__ import annotations

import json
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
    serialize_managed_state,
)
from news_bulletin_playlist.models import (
    AdapterId,
    DestinationReference,
    DurationPolicy,
    SourceId,
)


def test_builtin_catalog_has_stable_first_template_and_supported_sources() -> None:
    assert [str(source.id) for source in BUILTIN_CATALOG.sources] == [
        "ser",
        "rne",
        "ondacero",
        "abc",
        "cnn",
        "un_news_en",
        "reuters_world",
        "nplus_univision",
        "dw_actualidad",
        "un_news_es",
        "rfi_fr",
        "dlf_news",
        "rmf_fakty",
    ]
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    assert template.display_name == "Noticias en Español"
    assert "48h" not in template.display_name
    assert template.cover_id == "spain_spanish_news"
    assert tuple(map(str, template.languages)) == ("es-ES",)
    assert template.default_source_ids == (
        SourceId("ser"),
        SourceId("rne"),
        SourceId("ondacero"),
        SourceId("abc"),
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


def test_builtin_source_origin_is_not_copied_into_managed_playlists() -> None:
    template = BUILTIN_CATALOG.playlist("international_english_news")
    managed = activate_template(template, "spotify-playlist-id")
    payload = serialize_managed_state(ManagedState(playlists=(managed,)))

    assert payload["schema_version"] == 6
    assert payload["sources"] == []
    assert payload["playlists"][0]["source_ids"] == ["un_news_en", "reuters_world"]
    assert "origin" not in json.dumps(payload)
    assert "countries" not in json.dumps(payload)


def test_managed_state_rejects_duplicate_playlist_ids_before_write(tmp_path: Path) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = activate_template(template, "spotify-playlist-id")
    store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)

    with pytest.raises(ManagedStateError, match="managed playlist ids must be unique"):
        store.save(ManagedState(playlists=(managed, managed)))


def test_managed_state_rejects_duplicate_enabled_destination_before_write(
    tmp_path: Path,
) -> None:
    first_template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    second_template = BUILTIN_CATALOG.playlist("international_spanish_news")
    first = activate_template(first_template, "shared-destination")
    second = activate_template(second_template, "other-destination")
    second = replace(second, destination=first.destination)
    store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)

    with pytest.raises(ManagedStateError, match="duplicate enabled destination"):
        store.save(ManagedState(playlists=(first, second)))


def test_managed_state_rejects_enabled_playlist_without_sources_before_write(
    tmp_path: Path,
) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = replace(activate_template(template, "spotify-playlist-id"), source_ids=())
    store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)

    with pytest.raises(ManagedStateError, match="must select a source"):
        store.save(ManagedState(playlists=(managed,)))


def test_managed_compile_rejects_unknown_source() -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = replace(
        activate_template(template, "spotify-playlist-id"),
        source_ids=(SourceId("missing"),),
    )

    with pytest.raises(ManagedStateError, match="references unknown source"):
        compile_engine_config(BUILTIN_CATALOG, ManagedState(playlists=(managed,)))


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
    assert '"schema_version": 6' in raw
    assert '"sources": []' in raw
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


def _legacy_document(*, max_duration_line: str = "") -> str:
    return f"""{{
  "schema_version": 1,
  "playlists": [{{
    "id": "spain_spanish_news",
    "template_id": "spain_spanish_news",
    "enabled": true,
    "display_name": "Noticias España",
    "description": "legacy",
    "cover_id": "spain_spanish_news",
    "source_ids": ["ser"],
    "destination": {{"adapter_id": "spotify", "external_id": "legacy-destination"}},
    "retention_hours": 48,
    "max_episodes": 100{max_duration_line}
  }}]
}}
"""


def test_legacy_managed_state_without_duration_is_migrated_and_pinned(tmp_path: Path) -> None:
    path = tmp_path / MANAGED_STATE_FILENAME
    path.write_text(_legacy_document(), encoding="utf-8")

    state = ManagedStateStore(path).load()

    assert state.schema_version == 6
    assert state.sources == ()
    assert state.playlists[0].max_duration_seconds == 1800
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 6
    assert persisted["sources"] == []
    assert persisted["playlists"][0]["max_duration_seconds"] == 1800

    old_template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    changed_template = replace(
        old_template,
        duration_policy=DurationPolicy(default_max_seconds=900),
    )
    changed_catalog = BuiltInCatalog(
        sources=BUILTIN_CATALOG.sources,
        playlists=tuple(
            changed_template if template.id == old_template.id else template
            for template in BUILTIN_CATALOG.playlists
        ),
    )
    compiled = compile_engine_config(changed_catalog, state)
    assert compiled.playlists[0].duration_policy.default_max_seconds == 1800


def test_legacy_managed_state_preserves_explicit_duration_during_migration(tmp_path: Path) -> None:
    path = tmp_path / MANAGED_STATE_FILENAME
    path.write_text(
        _legacy_document(max_duration_line=',\n    "max_duration_seconds": 1234'),
        encoding="utf-8",
    )

    state = ManagedStateStore(path).load()

    assert state.schema_version == 6
    assert state.sources == ()
    assert state.playlists[0].max_duration_seconds == 1234
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["playlists"][0]["max_duration_seconds"] == 1234


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_migration_failure_preserves_original_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    path = tmp_path / MANAGED_STATE_FILENAME
    document = _legacy_document(
        max_duration_line=',\n    "max_duration_seconds": 1800' if version == 2 else ""
    ).replace('"schema_version": 1', f'"schema_version": {version}')
    original = document.encode()
    path.write_bytes(original)

    def fail_replace(source: object, destination: object) -> None:
        del source, destination
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr("news_bulletin_playlist.managed_state.os.replace", fail_replace)

    with pytest.raises(ManagedStateError, match="migration could not be persisted"):
        ManagedStateStore(path).load()

    assert path.read_bytes() == original


def test_schema_v2_requires_materialized_duration(tmp_path: Path) -> None:
    path = tmp_path / MANAGED_STATE_FILENAME
    document = _legacy_document().replace('"schema_version": 1', '"schema_version": 2')
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ManagedStateError, match="max_duration_seconds.*required"):
        ManagedStateStore(path).load()


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("fail", [False, True])
def test_all_legacy_schemas_migrate_exact_range_atomically(tmp_path, monkeypatch, version, fail):
    payload = serialize_managed_state(
        ManagedState(
            playlists=(
                replace(
                    activate_template(BUILTIN_CATALOG.playlists[0], "destination"),
                    max_duration_seconds=1234,
                ),
            )
        )
    )
    payload["schema_version"] = version
    payload["playlists"][0].pop("min_duration_seconds")
    if version < 5:
        payload.pop("source_overrides")
    if version < 3:
        payload.pop("sources")
    path = tmp_path / "managed-state.json"
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    if fail:

        def fail_replace(*args):
            raise OSError("replace failed")

        monkeypatch.setattr("news_bulletin_playlist.managed_state.os.replace", fail_replace)
        with pytest.raises(ManagedStateError, match="original state preserved"):
            ManagedStateStore(path).load()
        assert path.read_bytes() == original
    else:
        state = ManagedStateStore(path).load()
        assert state.schema_version == 6
        assert state.playlists[0].min_duration_seconds == 0
        assert state.playlists[0].max_duration_seconds == 1234
        assert json.loads(path.read_text())["playlists"][0]["min_duration_seconds"] == 0


def test_v6_requires_minimum(tmp_path):
    payload = serialize_managed_state(
        ManagedState(playlists=(activate_template(BUILTIN_CATALOG.playlists[0], "destination"),))
    )
    payload["playlists"][0].pop("min_duration_seconds")
    path = tmp_path / "managed-state.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ManagedStateError, match="min_duration_seconds.*required"):
        ManagedStateStore(path).load()
