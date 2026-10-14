"""Opt-in local-template state schema: v6 never migrates merely on load."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    activate_template,
    compile_engine_config,
    parse_managed_state,
    serialize_managed_state,
)
from news_bulletin_playlist.models import (
    LanguageTag,
    PlaylistId,
    SourceId,
)


def _draft() -> LocalPlaylistTemplate:
    return LocalPlaylistTemplate(
        id=PlaylistId("custom_world_briefs"),
        display_name="My world briefs",
        description="Independent world-news updates.",
        default_source_ids=(SourceId("cnn"), SourceId("un_news_en")),
        cover_id="international_spanish_news",
        languages=(LanguageTag("es-ES"),),
        retention_hours=72,
        max_episodes=40,
        min_duration_seconds=60,
        max_duration_seconds=900,
    )


def _existing_playlists():
    return tuple(
        activate_template(BUILTIN_CATALOG.playlist(key), f"spotify-destination-{i}")
        for i, key in enumerate(
            (
                "spain_spanish_news",
                "international_english_news",
                "international_polish_news",
                "international_spanish_long_news",
            )
        )
    )


def test_existing_v6_stays_v6_and_preserves_all_destinations(tmp_path: Path) -> None:
    path = tmp_path / MANAGED_STATE_FILENAME
    original = ManagedState(playlists=_existing_playlists())
    store = ManagedStateStore(path)
    store.save(original)
    old_bytes = path.read_bytes()

    assert store.load() == original
    assert path.read_bytes() == old_bytes
    assert json.loads(old_bytes)["schema_version"] == 6
    assert "templates" not in json.loads(old_bytes)

    service = ManagedAdminService(store)
    service.stop_managing("spain_spanish_news")
    assert store.load().schema_version == 6
    assert "templates" not in json.loads(path.read_bytes())


def test_opt_in_v7_stores_custom_draft_and_preserves_existing_playlists(
    tmp_path: Path,
) -> None:
    store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)
    old = ManagedState(playlists=_existing_playlists())
    store.save(old)

    updated = replace(old, schema_version=7, templates=(_draft(),))
    store.save(updated)
    saved = json.loads(store.path.read_text())
    assert saved["schema_version"] == 7
    assert len(saved["templates"]) == 1
    assert saved["templates"][0]["id"] == "custom_world_briefs"
    assert saved["templates"][0]["max_duration_seconds"] == 900
    assert saved["playlists"] == serialize_managed_state(old)["playlists"]
    assert store.load() == updated

    # Local lifecycle of an unrelated Spotify destination must not drop drafts.
    ManagedAdminService(store).stop_managing("spain_spanish_news")
    latest = store.load()
    assert latest.schema_version == 7
    assert latest.templates == (_draft(),)
    assert len(latest.playlists) == 3


def test_compilation_accepts_an_explicitly_instantiated_custom_template() -> None:
    draft = _draft()
    managed = activate_template(draft.as_playlist_template(), "spotify-test-destination")
    state = ManagedState(schema_version=7, templates=(draft,), playlists=(managed,))
    result = compile_engine_config(BUILTIN_CATALOG, state)

    assert result.playlists[0].id == draft.id
    assert result.playlists[0].retention_hours == 72
    assert result.playlists[0].max_episodes == 40
    assert result.playlists[0].source_selection.explicit == draft.default_source_ids
    assert result.playlists[0].duration_policy.default_min_seconds == 60
    assert result.playlists[0].duration_policy.default_max_seconds == 900


def test_schema_v6_never_silently_discards_new_templates() -> None:
    with pytest.raises(ManagedStateError, match="require.*v7"):
        serialize_managed_state(ManagedState(templates=(_draft(),)))

    payload = serialize_managed_state(ManagedState())
    payload["templates"] = []
    with pytest.raises(ManagedStateError, match="unknown key"):
        parse_managed_state(payload)


def test_v7_requires_templates_and_quarantines_broken_draft_without_truncation() -> None:
    state = ManagedState(schema_version=7, templates=(_draft(),))
    payload = serialize_managed_state(state)
    del payload["templates"]
    with pytest.raises(ManagedStateError, match="templates.*required"):
        parse_managed_state(payload)

    payload = serialize_managed_state(state)
    payload["templates"][0]["max_duration_seconds"] = -1
    loaded = parse_managed_state(payload)
    assert loaded.templates == ()
    assert len(loaded.quarantined_templates) == 1
    raw = json.loads(loaded.quarantined_templates[0].raw_json)
    assert raw["max_duration_seconds"] == -1


def test_v7_quarantines_duplicate_or_missing_source_without_accepting_them() -> None:
    draft = _draft()
    # Programmatic writes of non-canonical states still fail closed.
    with pytest.raises(ManagedStateError, match="non-canonical"):
        serialize_managed_state(ManagedState(schema_version=7, templates=(draft, draft)))
    bad = replace(draft, default_source_ids=(SourceId("uninstalled_source"),))
    with pytest.raises(ManagedStateError, match="non-canonical"):
        serialize_managed_state(ManagedState(schema_version=7, templates=(bad,)))

    payload = serialize_managed_state(ManagedState(schema_version=7, templates=(draft,)))
    payload["templates"].append(dict(payload["templates"][0]))
    parsed = parse_managed_state(payload)
    assert parsed.templates == ()
    assert len(parsed.quarantined_templates) == 2
    assert all(item.reason == "duplicate_id" for item in parsed.quarantined_templates)


def test_explicit_v7_write_failure_keeps_original_v6_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)
    existing = ManagedState(playlists=_existing_playlists())
    store.save(existing)
    original_bytes = store.path.read_bytes()

    def fail_replace(*_args: object) -> None:
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr("news_bulletin_playlist.managed_state.os.replace", fail_replace)
    with pytest.raises(OSError, match="filesystem failure"):
        store.save(replace(existing, schema_version=7, templates=(_draft(),)))
    assert store.path.read_bytes() == original_bytes
