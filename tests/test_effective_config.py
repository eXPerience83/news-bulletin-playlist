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
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_storage import (
    V11_AUTHORITY_FILENAME,
    V11_OPERATIONAL_FILENAME,
    V11OperationalStore,
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

    playlist = replace(
        activate_template(BUILTIN_CATALOG.playlist("spain_spanish_news"), "managed-destination"),
        enabled=False,
    )
    open_managed_state_store(tmp_path).save(ManagedState(playlists=(playlist,)))
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
    owner = open_managed_state_store(tmp_path)
    owner.save(replace(owner.load(), playlists=(paused,)))

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
    store.save(legacy)  # Supported dormant assignments retain their durable contract.
    assert store.load() == legacy
    assert legacy.playlists[0].enabled is True
    assert not legacy.sources[0].enabled
    with pytest.raises(ManagedStateError, match="at least one enabled source"):
        compile_engine_config(BUILTIN_CATALOG, legacy)

    original_bytes = store.path.read_bytes()
    assert _load_runtime_config(tmp_path, {}) is None
    recovered = open_managed_state_store(tmp_path).load()
    assert store.path.read_bytes() == original_bytes
    assert recovered == replace(legacy, playlists=(replace(legacy.playlists[0], enabled=False),))
    assert recovered.schema_version == legacy.schema_version == 6
    assert (
        compile_engine_config(BUILTIN_CATALOG, recovered).playlists[0].source_selection.explicit
        == ()
    )
    before = (tmp_path / V11_OPERATIONAL_FILENAME).read_bytes()

    def no_repeated_write(self: V11OperationalStore, *args: object, **kwargs: object) -> None:
        raise AssertionError("recovery must persist once, not on every runtime load")

    monkeypatch.setattr(V11OperationalStore, "save", no_repeated_write)
    assert _load_runtime_config(tmp_path, {}) is None
    assert (tmp_path / V11_OPERATIONAL_FILENAME).read_bytes() == before


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
    assert open_managed_state_store(tmp_path).load().playlists[:2] == tuple(
        replace(playlist, enabled=False) for playlist in (legacy.playlists[0], other_dormant)
    )
    assert config.playlists[2].enabled
    assert config.playlists[2].source_selection.explicit == healthy.source_ids
    assert open_managed_state_store(tmp_path).load().playlists[2] == healthy
    assert store.path.exists()


def test_legacy_dormant_recovery_replace_failure_preserves_original_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedStateStore(tmp_path / MANAGED_STATE_FILENAME)
    store.save(_legacy_dormant_state())
    before = store.path.read_bytes()

    import os

    real_link = os.link

    def fail_publication(source: str, target: str, **kwargs: object) -> None:
        if target == V11_OPERATIONAL_FILENAME:
            raise OSError("private-path-sentinel")
        real_link(source, target, **kwargs)

    monkeypatch.setattr("news_bulletin_playlist.v11_operational_storage.os.link", fail_publication)
    with pytest.raises(RuntimeError, match="invalid managed configuration") as caught:
        load_effective_config(tmp_path, {})
    assert "private-path-sentinel" not in str(caught.value)
    assert store.path.read_bytes() == before
    assert (tmp_path / V11_AUTHORITY_FILENAME).is_file()
    assert not (tmp_path / V11_OPERATIONAL_FILENAME).exists()
    with pytest.raises(RuntimeError, match="invalid managed configuration"):
        load_effective_config(tmp_path, {})


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
        with pytest.raises(RuntimeError, match="invalid managed configuration"):
            load_effective_config(tmp_path, {})
        assert store.path.read_bytes() == before
