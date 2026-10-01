from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.effective_config import load_effective_config
from news_bulletin_playlist.engine_runtime import _load_runtime_config
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedSource,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.models import (
    CountryCode,
    EditorialScope,
    LanguageTag,
    ParserId,
    PlaylistId,
    SourceId,
    SourceOrigin,
)


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


def _legacy_dormant_state() -> ManagedState:
    source = ManagedSource(
        id=SourceId("custom_" + "A" * 22),
        spotify_show_id="A" * 22,
        display_name="Legacy source",
        origin=SourceOrigin.country(CountryCode("ES")),
        language=LanguageTag("es"),
        editorial_scope=EditorialScope.NATIONAL,
        timezone="Europe/Madrid",
        parser_id=ParserId("release_date_title"),
        collection_filter_id=None,
        enabled=False,
    )
    playlist = replace(
        activate_template(BUILTIN_CATALOG.playlist("spain_spanish_news"), "legacy-destination"),
        source_ids=(source.id,),
    )
    return ManagedState(sources=(source,), playlists=(playlist,))


def test_runtime_load_atomically_pauses_legacy_active_playlist_with_only_disabled_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _legacy_dormant_state()
    store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)
    store.save(legacy)  # Supported schema v5: valid on main before #451.
    assert store.load() == legacy
    assert legacy.playlists[0].enabled is True
    assert not legacy.sources[0].enabled
    with pytest.raises(ManagedStateError, match="at least one enabled source"):
        compile_engine_config(BUILTIN_CATALOG, legacy)

    assert _load_runtime_config(tmp_path, {}) is None
    recovered = store.load()
    assert recovered == replace(legacy, playlists=(replace(legacy.playlists[0], enabled=False),))
    assert recovered.schema_version == legacy.schema_version == 5
    assert (
        compile_engine_config(BUILTIN_CATALOG, recovered).playlists[0].source_selection.explicit
        == ()
    )
    before = store.path.read_bytes()

    def no_repeated_write(self: ManagedStateStore, state: ManagedState) -> None:
        raise AssertionError("recovery must persist once, not on every runtime load")

    monkeypatch.setattr(ManagedStateStore, "save", no_repeated_write)
    assert _load_runtime_config(tmp_path, {}) is None
    assert store.path.read_bytes() == before  # Idempotent across cycles and restarts.


def test_legacy_dormant_recovery_keeps_independent_active_playlist(tmp_path: Path) -> None:
    legacy = _legacy_dormant_state()
    healthy = activate_template(
        BUILTIN_CATALOG.playlist("international_english_news"), "independent-destination"
    )
    store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)
    other_dormant = replace(
        legacy.playlists[0],
        id=PlaylistId("other-dormant"),
        destination=replace(legacy.playlists[0].destination, external_id="other-destination"),
    )
    store.save(replace(legacy, playlists=legacy.playlists + (other_dormant, healthy)))
    config = load_effective_config(tmp_path, {})
    assert config is not None
    assert not config.playlists[0].enabled
    assert config.playlists[0].source_selection.explicit == ()
    assert not config.playlists[1].enabled
    assert store.load().playlists[:2] == tuple(
        replace(playlist, enabled=False) for playlist in (legacy.playlists[0], other_dormant)
    )
    assert config.playlists[2].enabled
    assert config.playlists[2].source_selection.explicit == healthy.source_ids
    assert store.load().playlists[2] == healthy


def test_legacy_dormant_recovery_replace_failure_preserves_original_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)
    store.save(_legacy_dormant_state())
    before = store.path.read_bytes()

    def fail_replace(*args: object) -> None:
        raise OSError("private-path-sentinel")

    monkeypatch.setattr("news_bulletin_playlist.managed_state.os.replace", fail_replace)
    with pytest.raises(RuntimeError, match="recovery could not be persisted safely") as caught:
        load_effective_config(tmp_path, {})
    assert "private-path-sentinel" not in str(caught.value)
    assert store.path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [store.path]


def test_legacy_dormant_recovery_does_not_mask_unknown_source_or_template(tmp_path: Path) -> None:
    legacy = _legacy_dormant_state()
    store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)
    for playlist in (
        replace(
            legacy.playlists[0], source_ids=legacy.playlists[0].source_ids + (SourceId("missing"),)
        ),
        replace(legacy.playlists[0], template_id=PlaylistId("missing")),
    ):
        store.save(replace(legacy, playlists=(playlist,)))
        before = store.path.read_bytes()
        with pytest.raises(RuntimeError, match="references unknown"):
            load_effective_config(tmp_path, {})
        assert store.path.read_bytes() == before
