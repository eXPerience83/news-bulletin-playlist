from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.engine import EngineRunner, _required_sources
from news_bulletin_playlist.engine_runtime import (
    ConfigurationSynchronization,
    ReloadingEngineCycleRunner,
    _playlist_write_contract,
)
from news_bulletin_playlist.managed_admin import ManagedAdminError, ManagedAdminService
from news_bulletin_playlist.managed_provisioning import ProvisioningIntent, ProvisioningState
from news_bulletin_playlist.managed_state import (
    ManagedSource,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    activate_template,
    compile_engine_config,
    effective_sources,
    parse_managed_state,
    serialize_managed_state,
)
from news_bulletin_playlist.models import (
    CountryCode,
    EditorialScope,
    ExternalReference,
    LanguageTag,
    ParserId,
    SourceId,
    SourceOrigin,
)
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.reconciliation import SpotifyReconciliationError
from news_bulletin_playlist.source_admin import SourceAdminService
from news_bulletin_playlist.spotify.auth import SpotifyAuthError
from news_bulletin_playlist.spotify.first_desired import build_spotify_first_desired_state
from news_bulletin_playlist.spotify.first_models import (
    RefreshReason,
    SpotifyFirstEpisode,
    TimestampSource,
)
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetectorCycleSummary,
    PublicDetectorJournal,
)
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal

SHOW_A = "A" * 22
SHOW_B = "B" * 22
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def _source(show_id: str = SHOW_A, *, enabled: bool = True) -> ManagedSource:
    return ManagedSource(
        id=SourceId(f"custom_{show_id}"),
        spotify_show_id=show_id,
        display_name="Custom News",
        origin=SourceOrigin.country(CountryCode("ES")),
        language=LanguageTag("es-ES"),
        editorial_scope=EditorialScope.INTERNATIONAL,
        timezone="Europe/Madrid",
        parser_id=ParserId("release_date_title"),
        collection_filter_id=None,
        enabled=enabled,
    )


def _state(source: ManagedSource | None = None) -> ManagedState:
    selected = _source() if source is None else source
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    playlist = replace(
        activate_template(template, "destination"),
        source_ids=(selected.id,),
    )
    return ManagedState(sources=(selected,), playlists=(playlist,))


def _payload() -> dict[str, object]:
    return serialize_managed_state(_state())


def test_v3_round_trip_compiles_custom_without_changing_builtins(tmp_path: Path) -> None:
    path = tmp_path / "managed-state.json"
    store = ManagedStateStore(path)
    state = _state()
    store.save(state)

    assert ManagedStateStore(path).load() == state
    expected = json.dumps(_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert path.read_bytes() == expected.encode()
    compiled = compile_engine_config(BUILTIN_CATALOG, state)
    assert compiled.sources[: len(BUILTIN_CATALOG.sources)] == BUILTIN_CATALOG.sources
    custom = compiled.sources[-1]
    assert custom.id == SourceId(f"custom_{SHOW_A}")
    assert custom.countries == ()
    assert custom.origin == SourceOrigin.country(CountryCode("ES"))
    assert custom.languages == (LanguageTag("es-ES"),)
    assert custom.editorial_scope is EditorialScope.INTERNATIONAL
    assert custom.external_references[0].external_id == SHOW_A
    assert compiled.playlists[0].source_selection.explicit == (custom.id,)
    assert "access_token" not in path.read_text()
    assert "share_url" not in path.read_text()


def test_institutional_origin_and_deterministic_custom_order() -> None:
    institutional = replace(
        _source(SHOW_B), origin=SourceOrigin.institution("UN"), language=LanguageTag("en")
    )
    state = ManagedState(sources=(institutional, _source()))
    parsed = parse_managed_state(serialize_managed_state(state))
    assert parsed.sources[-1].origin == SourceOrigin.institution("UN")
    assert serialize_managed_state(state) == serialize_managed_state(
        replace(state, sources=tuple(reversed(state.sources)))
    )
    assert [source.id for source in effective_sources(BUILTIN_CATALOG, parsed)[-2:]] == [
        SourceId(f"custom_{SHOW_A}"),
        SourceId(f"custom_{SHOW_B}"),
    ]


@pytest.mark.parametrize("language", ["en", "es", "es-ES", "es-419"])
def test_custom_source_language_tags_round_trip(language: str, tmp_path: Path) -> None:
    source = replace(_source(), language=LanguageTag(language))
    store = ManagedStateStore(tmp_path / "managed-state.json")
    store.save(_state(source))

    loaded = ManagedStateStore(store.path).load()
    assert loaded.sources[0].language == LanguageTag(language)
    assert serialize_managed_state(loaded)["sources"][0]["language"] == language
    assert compile_engine_config(BUILTIN_CATALOG, loaded).sources[-1].languages == (
        LanguageTag(language),
    )


def test_no_custom_sources_preserve_exact_builtin_definitions() -> None:
    compiled = compile_engine_config(BUILTIN_CATALOG, ManagedState())
    assert compiled.sources == BUILTIN_CATALOG.sources
    assert all(
        actual is original
        for actual, original in zip(compiled.sources, BUILTIN_CATALOG.sources, strict=True)
    )
    assert compiled.playlists == ()


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_schema_migrates_atomically_with_empty_sources(tmp_path: Path, version: int) -> None:
    path = tmp_path / "managed-state.json"
    playlist = _payload()["playlists"][0]
    playlist["source_ids"] = ["ser"]
    if version == 1:
        del playlist["max_duration_seconds"]
    original = {"schema_version": version, "playlists": [playlist]}
    path.write_text(json.dumps(original), encoding="utf-8")

    state = ManagedStateStore(path).load()

    persisted = json.loads(path.read_text())
    assert state.schema_version == persisted["schema_version"] == 3
    assert state.sources == ()
    assert persisted["sources"] == []
    assert persisted["playlists"][0]["source_ids"] == ["ser"]
    assert persisted["playlists"][0]["max_duration_seconds"] == 1800


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_schema_rejects_v3_sources_key(version: int) -> None:
    payload = _payload()
    payload["schema_version"] = version
    with pytest.raises(ManagedStateError, match="unknown key 'sources'"):
        parse_managed_state(payload)


@pytest.mark.parametrize("location", ["root", "source", "origin"])
def test_v3_rejects_unknown_keys(location: str) -> None:
    payload = _payload()
    target = (
        payload
        if location == "root"
        else payload["sources"][0]
        if location == "source"
        else payload["sources"][0]["origin"]
    )
    target["unexpected"] = True
    with pytest.raises(ManagedStateError, match="unknown key"):
        parse_managed_state(payload)


def test_v3_requires_sources_and_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    payload = _payload()
    del payload["sources"]
    with pytest.raises(ManagedStateError, match="sources.*required"):
        parse_managed_state(payload)
    path = tmp_path / "managed-state.json"
    path.write_text('{"schema_version":3,"sources":[],"sources":[],"playlists":[]}')
    with pytest.raises(ManagedStateError, match="duplicate JSON key: sources"):
        ManagedStateStore(path).load()


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("id", "custom_wrong", "id"),
        ("spotify_show_id", "https://open.spotify.com/show/A", "spotify_show_id"),
        ("display_name", "", "display_name"),
        ("display_name", "x" * 121, "display_name"),
        ("language", "", "language"),
        ("language", "en_US", "language"),
        ("language", "ES", "language"),
        ("language", "es-es", "language"),
        ("language", "es-41", "language"),
        ("language", "es-4199", "language"),
        ("language", "es-Latn", "language"),
        ("editorial_scope", "WORLD", "editorial_scope"),
        ("timezone", "Mars/Olympus", "timezone"),
        ("timezone", "A" * 129, "timezone"),
        ("parser_id", "ser", "parser_id"),
        ("collection_filter_id", "un_news_es_minutes", "collection_filter_id"),
        ("enabled", "false", "enabled"),
    ],
)
def test_invalid_custom_source_fails_closed(field: str, value: object, expected: str) -> None:
    payload = _payload()
    payload["sources"][0][field] = value
    with pytest.raises(ManagedStateError, match=expected):
        parse_managed_state(payload)


def test_display_name_is_trimmed_and_length_boundary_is_accepted() -> None:
    payload = _payload()
    payload["sources"][0]["display_name"] = " " + ("x" * 120) + " "
    assert parse_managed_state(payload).sources[0].display_name == "x" * 120


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ({"kind": "planet", "code": "ES"}, "origin"),
        ({"kind": "country", "code": "UN"}, "origin"),
        ({"kind": "institution", "code": "uN"}, "origin"),
        ({"kind": "institution"}, "origin.code"),
    ],
)
def test_invalid_origin_fails_closed(origin: object, expected: str) -> None:
    payload = _payload()
    payload["sources"][0]["origin"] = origin
    with pytest.raises(ManagedStateError, match=expected):
        parse_managed_state(payload)


def test_duplicate_custom_sources_fail_closed() -> None:
    payload = _payload()
    payload["sources"].append(payload["sources"][0].copy())
    with pytest.raises(ManagedStateError, match=r"sources\[1\].id: duplicate"):
        parse_managed_state(payload)


def test_v3_atomic_replace_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "managed-state.json"
    store = ManagedStateStore(path)
    store.save(_state())
    original = path.read_bytes()

    def fail_replace(source: object, destination: object) -> None:
        del source, destination
        raise OSError("simulated replace failure")

    monkeypatch.setattr("news_bulletin_playlist.managed_state.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.save(ManagedState())
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_builtin_custom_id_and_show_collisions_fail_closed() -> None:
    built_in = BUILTIN_CATALOG.sources[0]
    by_id = replace(_source(), id=built_in.id)
    with pytest.raises(ManagedStateError, match="id"):
        effective_sources(BUILTIN_CATALOG, ManagedState(sources=(by_id,)))
    by_show = replace(
        _source(built_in.external_references[0].external_id),
    )
    with pytest.raises(ManagedStateError, match="built-in Spotify show"):
        effective_sources(BUILTIN_CATALOG, ManagedState(sources=(by_show,)))
    future_builtin = replace(
        built_in,
        external_references=(ExternalReference("spotify", "show", by_show.spotify_show_id),),
    )
    future_catalog = BuiltInCatalog(
        sources=(future_builtin,) + BUILTIN_CATALOG.sources[1:],
        playlists=BUILTIN_CATALOG.playlists,
    )
    with pytest.raises(ManagedStateError, match="built-in Spotify show"):
        effective_sources(future_catalog, ManagedState(sources=(by_show,)))
    future_id_source = replace(built_in, id=_source().id)
    future_id_catalog = BuiltInCatalog(
        sources=BUILTIN_CATALOG.sources + (future_id_source,),
        playlists=BUILTIN_CATALOG.playlists,
    )
    with pytest.raises(ManagedStateError, match="conflicts with built-in source"):
        effective_sources(future_id_catalog, ManagedState(sources=(_source(),)))


def test_disabled_selected_source_never_authorizes_old_episode_state() -> None:
    state = _state(_source(enabled=False))
    config = compile_engine_config(BUILTIN_CATALOG, state)
    assert config.playlists[0].source_selection.explicit == (state.sources[0].id,)
    assert _required_sources(config) == ()
    runner = object.__new__(EngineRunner)
    runner.config = config

    class OldCoverage:
        def get_source_state(self, source_id: SourceId) -> object:
            raise AssertionError(f"old SQLite source state must not authorize {source_id}")

    runner.first_journal = OldCoverage()  # type: ignore[assignment]
    assert not runner._playlist_sources_ready(config.playlists[0], now=NOW)

    class MissingCoverage:
        def get_source_state(self, source_id: SourceId) -> None:
            assert source_id == state.sources[0].id
            return None

    runner.config = compile_engine_config(BUILTIN_CATALOG, _state())
    runner.first_journal = MissingCoverage()  # type: ignore[assignment]
    assert not runner._playlist_sources_ready(runner.config.playlists[0], now=NOW)

    with pytest.raises(ValueError, match="missing or disabled"):
        build_spotify_first_desired_state(
            config.playlists[0],
            (),
            sources={source.id: source for source in config.sources},
            now=NOW,
        )
    with pytest.raises(ValueError, match="missing or disabled"):
        build_spotify_first_desired_state(config.playlists[0], (), sources={}, now=NOW)


def test_shared_custom_source_is_observed_and_planned_only_once_per_cycle(tmp_path: Path) -> None:
    custom = _source()
    first_playlist = _state(custom).playlists[0]
    second_playlist = replace(
        activate_template(BUILTIN_CATALOG.playlist("international_english_news"), "destination-2"),
        source_ids=(custom.id,),
    )
    config = compile_engine_config(
        BUILTIN_CATALOG,
        ManagedState(sources=(custom,), playlists=(first_playlist, second_playlist)),
    )
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    first = SpotifyFirstJournal(store)
    first.initialize()
    detector_journal = PublicDetectorJournal(store)
    detector_journal.initialize()

    class Detector:
        def __init__(self) -> None:
            self.calls = 0

        def observe_sources(
            self, sources: object, *, observed_at: datetime
        ) -> PublicDetectorCycleSummary:
            self.calls += 1
            assert tuple(source.id for source in sources) == (custom.id,)  # type: ignore[attr-defined]
            return PublicDetectorCycleSummary(1, 0, 1, 0)

    class UnavailableAuth:
        def __init__(self) -> None:
            self.calls = 0

        def acquire_provider_session(self, *, now: datetime) -> None:
            self.calls += 1
            raise SpotifyAuthError("test authorization unavailable")

    detector = Detector()
    auth = UnavailableAuth()
    runner = EngineRunner(
        config,
        store,
        auth,
        detector,
        detector_journal,
        first,
        client_factory=lambda token: pytest.fail(f"unexpected provider client: {token}"),
        clock=lambda: NOW,
    )  # type: ignore[arg-type]
    result = runner.run_cycle()
    assert detector.calls == 1
    assert auth.calls == 1  # One bootstrap plan despite two playlists selecting the same source.
    assert len(result.sources) == 1 and result.sources[0].source_id == custom.id
    assert len(result.playlists) == 2


def test_pending_custom_assignment_round_trips_and_recovery_preserves_identity(
    tmp_path: Path,
) -> None:
    custom = _source()
    service = ManagedAdminService(ManagedStateStore(tmp_path / "managed-state.json"))
    service.store.save(ManagedState(sources=(custom,)))
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    intent = ProvisioningIntent(
        state=ProvisioningState.DESTINATION_KNOWN,
        template_id=template.id,
        display_name="Custom playlist",
        description="",
        cover_id=template.cover_id,
        source_ids=(custom.id,),
        retention_hours=template.retention_hours,
        max_episodes=template.max_episodes,
        max_duration_seconds=template.duration_policy.default_max_seconds,
        destination_id="destination",
    )
    service.provisioning_journal.save(intent)
    assert service.provisioning_journal.load() == intent
    recovered = service.finalize_known_provisioning()
    assert recovered is not None and recovered.source_ids == (custom.id,)
    assert service.store.load().playlists == (recovered,)
    assert service.provisioning_journal.load() is None

    # Recovery must not silently drop a custom source that became disabled before finalization.
    service.store.save(ManagedState(sources=(replace(custom, enabled=False),)))
    service.provisioning_journal.save(intent)
    with pytest.raises(ManagedAdminError, match="disabled source"):
        service.finalize_known_provisioning()
    assert service.store.load().playlists == ()
    assert service.provisioning_journal.load() == intent


def test_disabled_selected_source_preserves_destination_in_real_cycle(tmp_path: Path) -> None:
    state = _state(_source(enabled=False))
    config = compile_engine_config(BUILTIN_CATALOG, state)
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    first = SpotifyFirstJournal(store)
    first.initialize()
    detector_journal = PublicDetectorJournal(store)
    detector_journal.initialize()
    episode_id = "E" * 22
    first.replace_source_episodes(
        state.sources[0].id,
        (
            SpotifyFirstEpisode(
                source_id=state.sources[0].id,
                spotify_show_id=SHOW_A,
                spotify_episode_id=episode_id,
                spotify_episode_uri=f"spotify:episode:{episode_id}",
                title="Old persisted bulletin",
                edition_at=NOW,
                timestamp_source=TimestampSource.TITLE,
                release_date_raw="2026-09-24",
                release_date_precision="day",
                duration_seconds=300,
                first_seen_at=NOW,
                last_seen_at=NOW,
            ),
        ),
        coverage_proven=True,
    )
    first.record_refresh(
        config.sources[-1],
        refreshed_at=NOW,
        reason=RefreshReason.BOOTSTRAP,
        detector_episode_id=episode_id,
        api_episode_id=episode_id,
        request_count=1,
        complete=True,
        coverage_proven=True,
        coverage_cutoff_at=NOW - timedelta(hours=48),
    )

    class NoAuth:
        def acquire_provider_session(self, *, now: datetime) -> None:
            raise AssertionError(f"disabled source must not request Spotify auth at {now}")

    class NoDetector:
        def observe_sources(
            self, sources: object, *, observed_at: datetime
        ) -> PublicDetectorCycleSummary:
            assert tuple(sources) == ()
            assert observed_at == NOW
            return PublicDetectorCycleSummary(0, 0, 0, 0)

    def no_client(token: str) -> None:
        raise AssertionError(f"no Spotify client may be created: {token}")

    runner = EngineRunner(
        config,
        store,
        NoAuth(),
        NoDetector(),
        detector_journal,
        first,
        client_factory=no_client,
        clock=lambda: NOW,
    )  # type: ignore[arg-type]
    result = runner.run_cycle()
    assert result.sources == ()
    assert len(result.playlists) == 1
    assert result.playlists[0].wrote is None
    assert result.playlists[0].write_outcome == "not_attempted"
    assert "destination preserved" in (result.playlists[0].error or "")
    assert first.list_episodes(source_ids=(state.sources[0].id,))


def test_enabled_builtin_plus_disabled_custom_never_writes_reduced_destination(
    tmp_path: Path,
) -> None:
    custom = _source(enabled=False)
    state = _state(custom)
    state = replace(
        state,
        playlists=(replace(state.playlists[0], source_ids=(SourceId("ser"), custom.id)),),
    )
    config = compile_engine_config(BUILTIN_CATALOG, state)
    sqlite_store = SQLiteStore(tmp_path / "state.sqlite3")
    sqlite_store.initialize()
    first = SpotifyFirstJournal(sqlite_store)
    first.initialize()
    detector_journal = PublicDetectorJournal(sqlite_store)
    detector_journal.initialize()
    ser = next(source for source in config.sources if source.id == SourceId("ser"))
    first.record_refresh(
        ser,
        refreshed_at=NOW,
        reason=RefreshReason.BOOTSTRAP,
        detector_episode_id=None,
        api_episode_id=None,
        request_count=1,
        complete=True,
        coverage_proven=True,
        coverage_cutoff_at=NOW - timedelta(hours=48),
    )

    class NoAuth:
        def acquire_provider_session(self, *, now: datetime) -> None:
            raise AssertionError(f"no catalogue refresh expected at {now}")

    class Detector:
        def observe_sources(
            self, sources: object, *, observed_at: datetime
        ) -> PublicDetectorCycleSummary:
            assert tuple(source.id for source in sources) == (SourceId("ser"),)  # type: ignore[attr-defined]
            assert observed_at == NOW
            return PublicDetectorCycleSummary(0, 0, 0, 0)

    writes: list[str] = []

    def client_factory(token: str) -> None:
        writes.append(token)
        raise AssertionError("destination must not be mutated")

    runner = EngineRunner(
        config,
        sqlite_store,
        NoAuth(),
        Detector(),
        detector_journal,
        first,
        client_factory=client_factory,
        clock=lambda: NOW,
    )  # type: ignore[arg-type]
    result = runner.run_cycle()
    assert result.playlists[0].ok is False
    assert result.playlists[0].write_outcome == "not_attempted"
    assert "destination preserved" in (result.playlists[0].error or "")
    assert writes == []


def test_mid_cycle_disable_blocks_write_but_safe_metadata_change_does_not(
    tmp_path: Path,
) -> None:
    state = _state()
    original = compile_engine_config(BUILTIN_CATALOG, state)
    managed_store = ManagedStateStore(tmp_path / "managed-state.json")
    managed_store.save(state)
    synchronization = ConfigurationSynchronization()

    class NoAuth:
        def get_access_token(self, *, now: datetime | None = None) -> str:
            raise AssertionError(f"no provider call expected at {now}")

    service = SourceAdminService(
        managed_store,
        synchronization,
        NoAuth(),
        SpotifyRateLimitJournal(SQLiteStore(tmp_path / "rate-limit.sqlite3")),
        None,
    )
    runner = object.__new__(ReloadingEngineCycleRunner)
    runner.data_dir = tmp_path
    runner.environ = {}
    runner.configuration_synchronization = synchronization
    writes: list[str] = []

    service.set_enabled(str(state.sources[0].id), False)
    with (
        pytest.raises(SpotifyReconciliationError, match="destination preserved"),
        runner._playlist_mutation_guard(original, original.playlists[0]),
    ):
        writes.append("spotify mutation")
    assert writes == []

    service.set_enabled(str(state.sources[0].id), True)
    service.update_metadata(
        str(state.sources[0].id),
        display_name="Renamed",
        origin_kind="institution",
        origin_code="UN",
        language="en",
        editorial_scope="MIX",
    )
    with runner._playlist_mutation_guard(original, original.playlists[0]):
        writes.append("allowed")
    assert writes == ["allowed"]


def test_reenabled_source_uses_normal_coverage_readiness(tmp_path: Path) -> None:
    state = _state(_source(enabled=False))
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    first = SpotifyFirstJournal(store)
    first.initialize()
    runner = object.__new__(EngineRunner)
    runner.first_journal = first
    runner.config = compile_engine_config(BUILTIN_CATALOG, state)
    playlist = runner.config.playlists[0]
    assert not runner._playlist_sources_ready(playlist, now=NOW)

    runner.config = compile_engine_config(
        BUILTIN_CATALOG, replace(state, sources=(replace(state.sources[0], enabled=True),))
    )
    playlist = runner.config.playlists[0]
    assert not runner._playlist_sources_ready(playlist, now=NOW)
    source = next(source for source in runner.config.sources if source.id == state.sources[0].id)
    first.record_refresh(
        source,
        refreshed_at=NOW,
        reason=RefreshReason.BOOTSTRAP,
        detector_episode_id=None,
        api_episode_id=None,
        request_count=1,
        complete=True,
        coverage_proven=False,
        coverage_cutoff_at=NOW - timedelta(hours=48),
    )
    assert not runner._playlist_sources_ready(playlist, now=NOW)
    first.record_refresh(
        source,
        refreshed_at=NOW,
        reason=RefreshReason.BOOTSTRAP,
        detector_episode_id=None,
        api_episode_id=None,
        request_count=1,
        complete=True,
        coverage_proven=True,
        coverage_cutoff_at=NOW - timedelta(hours=48),
    )
    assert runner._playlist_sources_ready(playlist, now=NOW)


def test_source_semantics_change_write_contract_but_classification_does_not() -> None:
    base = _state()
    playlist_id = base.playlists[0].id
    contract = _playlist_write_contract(compile_engine_config(BUILTIN_CATALOG, base), playlist_id)
    source = base.sources[0]
    for changed in (replace(source, enabled=False), replace(source, timezone="UTC")):
        assert (
            _playlist_write_contract(
                compile_engine_config(BUILTIN_CATALOG, replace(base, sources=(changed,))),
                playlist_id,
            )
            != contract
        )
    compiled = compile_engine_config(BUILTIN_CATALOG, base)
    runtime_source = compiled.sources[-1]
    for changed in (
        replace(runtime_source, parser_id=ParserId("ser")),
        replace(runtime_source, collection_filter_id="un_news_es_minutes"),
        replace(
            runtime_source,
            external_references=(ExternalReference("spotify", "show", SHOW_B),),
        ),
    ):
        changed_config = replace(compiled, sources=compiled.sources[:-1] + (changed,))
        assert _playlist_write_contract(changed_config, playlist_id) != contract
    metadata = replace(
        source,
        display_name="Renamed",
        origin=SourceOrigin.institution("UN"),
        language=LanguageTag("en"),
        editorial_scope=EditorialScope.MIXED,
    )
    assert (
        _playlist_write_contract(
            compile_engine_config(BUILTIN_CATALOG, replace(base, sources=(metadata,))), playlist_id
        )
        == contract
    )


def test_admin_source_validation_uses_effective_set_and_preserves_custom_state(
    tmp_path: Path,
) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    state = _state()
    store.save(state)
    service = ManagedAdminService(store)
    current = state.playlists[0]
    service.update(
        current.id,
        display_name=current.display_name,
        description=current.description,
        cover_id=current.cover_id,
        source_ids=(state.sources[0].id,),
        enabled=True,
        access_token=None,
    )
    assert store.load().sources == state.sources
    disabled = replace(state.sources[0], enabled=False)
    unassigned_playlist = replace(current, source_ids=(SourceId("ser"),))
    store.save(replace(state, sources=(disabled,), playlists=(unassigned_playlist,)))
    with pytest.raises(ManagedAdminError, match="disabled source"):
        service.update(
            current.id,
            display_name=current.display_name,
            description=current.description,
            cover_id=current.cover_id,
            source_ids=(disabled.id,),
            enabled=True,
            access_token=None,
        )
    assert store.load().playlists == (unassigned_playlist,)
    store.save(replace(state, sources=(disabled,)))
    service.update(
        current.id,
        display_name=current.display_name,
        description=current.description,
        cover_id=current.cover_id,
        source_ids=(disabled.id,),
        enabled=True,
        access_token=None,
    )
    assert store.load().sources == (disabled,)
    with pytest.raises(ManagedAdminError, match="unknown catalog source"):
        service.update(
            current.id,
            display_name=current.display_name,
            description=current.description,
            cover_id=current.cover_id,
            source_ids=(SourceId("ser"), SourceId("custom_unknown")),
            enabled=True,
            access_token=None,
        )
    assert store.load().playlists[0].source_ids == (disabled.id,)
