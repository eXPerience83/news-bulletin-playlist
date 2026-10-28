"""Lossless installed capabilities, explicit v8 persistence and legacy isolation (#548)."""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from _spotify_catalogue import catalogue_page
from test_custom_playlist_templates import _draft
from test_managed_sources import _source, _state

from news_bulletin_playlist import managed_state
from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.custom_source_policy import custom_strategy_allowed
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_provisioning import ProvisioningIntent, ProvisioningState
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedSourceOverride,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    QuarantinedPlaylistTemplate,
    compile_engine_config,
    effective_sources,
    serialize_managed_state,
    source_definition,
)
from news_bulletin_playlist.models import (
    CountryCode,
    ExternalReference,
    LanguageTag,
    ParserId,
    PublicReleaseMode,
    SourceId,
)
from news_bulletin_playlist.portable_sources import export_sources, parse_sources
from news_bulletin_playlist.source_admin import SourceAdminService, export_source_configuration
from news_bulletin_playlist.spotify.auth import SpotifyAuthService
from news_bulletin_playlist.spotify.client import SpotifyClient
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.first_models import TimestampSource

EMPTY = BuiltInCatalog((), ())


@pytest.fixture(autouse=True)
def no_provider_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Installed source persistence must not access network/OAuth/Spotify")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(SpotifyAuthService, "get_access_token", forbidden)
    monkeypatch.setattr(SpotifyClient, "__init__", forbidden)


@pytest.mark.parametrize("definition", BUILTIN_CATALOG.sources, ids=lambda item: item.id)
def test_all_builtin_definitions_persist_without_loss(tmp_path, definition):
    # Inject a catalogue without image-owned sources; production defaults stay intact.
    store = ManagedStateStore(tmp_path / "state.json", catalog=EMPTY)
    installed = InstalledSource.from_definition(definition)
    state = ManagedState(schema_version=8, sources=(installed,))
    store.save(state)
    assert store.load() == state
    assert store.load_readonly() == state
    assert effective_sources(EMPTY, store.load()) == (definition,)
    document = json.loads(store.path.read_bytes())
    assert document["sources"] == []
    assert document["installed_sources"] == json.loads(export_sources((definition,)))
    assert export_sources(effective_sources(EMPTY, store.load())) == export_sources((definition,))
    assert document["installed_sources"]["sources"][0]["processing"]["parser"]["version"] == 1


def _installed(**overrides):
    definition = replace(
        BUILTIN_CATALOG.sources[0],
        id=SourceId("installed_news"),
        external_references=(ExternalReference("spotify", "show", "Z" * 22),),
        countries=(CountryCode("ES"), CountryCode("AR")),
        languages=(LanguageTag("es-ES"), LanguageTag("es-419"), LanguageTag("en")),
    )
    return InstalledSource.from_definition(replace(definition, **overrides))


@pytest.mark.parametrize("countries", [(), (CountryCode("ES"), CountryCode("AR"))])
@pytest.mark.parametrize("enabled", [True, False])
def test_multilingual_classifications_and_disabled_state(tmp_path, countries, enabled):
    source = _installed(countries=countries, enabled=enabled)
    store = ManagedStateStore(tmp_path / "state.json")
    store.save(ManagedState(schema_version=8, sources=(source, _source())))
    loaded = store.load()
    assert source in loaded.sources
    result = next(s for s in effective_sources(BUILTIN_CATALOG, loaded) if s.id == source.id)
    assert result == source_definition(source)
    assert result.countries == countries
    assert result.enabled is enabled
    assert len(result.languages) == 3


def test_opt_in_required_and_legacy_payloads_remain_unchanged(tmp_path, monkeypatch):
    for version in (6, 7):
        store = ManagedStateStore(tmp_path / f"state-{version}.json")
        original = replace(_state(), schema_version=version)
        store.save(original)
        before = store.path.read_bytes()
        with monkeypatch.context() as read_guard:

            def forbidden(*args, **kwargs):
                pytest.fail("Legacy v6/v7 reads must not save or migrate")

            read_guard.setattr(store, "save", forbidden)
            for read in (store.load, store.load_readonly):
                assert read() == original
                assert store.path.read_bytes() == before
        store.save(store.load())
        assert store.path.read_bytes() == before
        with pytest.raises(ManagedStateError, match="explicit.*v8"):
            store.save(replace(original, sources=(*original.sources, _installed())))
        assert store.path.read_bytes() == before
        opted_in = replace(original, schema_version=8, sources=(*original.sources, _installed()))
        store.save(opted_in)
        assert store.load() == opted_in
        assert store.load().playlists == original.playlists


def _v8_payload():
    return serialize_managed_state(ManagedState(schema_version=8, sources=(_installed(),)))


@pytest.mark.parametrize(
    "capability", ["parser", "temporal_strategy", "public_release_mode", "collection_filter"]
)
@pytest.mark.parametrize("bad", ["id", "version", "parameters"])
def test_unknown_capability_contracts_rejected_without_writes(tmp_path, capability, bad):
    store = ManagedStateStore(tmp_path / "state.json")
    payload = _v8_payload()
    processing = payload["installed_sources"]["sources"][0]["processing"]
    supported = {
        "parser": "ser",
        "temporal_strategy": "auto",
        "public_release_mode": "instant",
        "collection_filter": "un_news_es_minutes",
    }
    processing[capability] = {"id": supported[capability], "version": 1, "parameters": {}}
    processing[capability][bad] = {
        "id": "untrusted-secret-sentinel",
        "version": 2,
        "parameters": {"regex": ".*"},
    }[bad]
    store.path.write_text(json.dumps(payload))
    before = store.path.read_bytes()
    for read in (store.load, store.load_readonly):
        with pytest.raises(ManagedStateError, match="Invalid installed source document") as error:
            read()
        assert "secret-sentinel" not in str(error.value)
        assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        "truncated",
        "missing",
        "extra",
        "duplicate_key",
        "wrong_type",
        "duplicate_show",
        "duplicate_id",
    ],
)
def test_damaged_v8_state_fails_closed(tmp_path, change):
    payload = _v8_payload()
    if change == "missing":
        payload.pop("installed_sources")
    elif change == "extra":
        payload["installed_sources"]["sources"][0]["unexpected"] = True
    elif change == "wrong_type":
        payload["installed_sources"]["sources"][0]["enabled"] = "yes"
    elif change in {"duplicate_show", "duplicate_id"}:
        manual = _source()
        row = payload["installed_sources"]["sources"][0]
        row["spotify_show_id" if change == "duplicate_show" else "original_id"] = (
            manual.spotify_show_id if change == "duplicate_show" else manual.id
        )
        payload["sources"] = serialize_managed_state(ManagedState(sources=(manual,)))["sources"]
    raw = json.dumps(payload)
    if change == "truncated":
        raw = raw[:-5]
    if change == "duplicate_key":
        raw = raw.replace('"version": 1', '"version": 1, "version": 1', 1)
    store = ManagedStateStore(tmp_path / "state.json")
    store.path.write_text(raw)
    for read in (store.load, store.load_readonly):
        with pytest.raises(ManagedStateError):
            read()
        assert store.path.read_text() == raw


def test_builtin_identity_collisions_still_fail_closed(tmp_path):
    installed = InstalledSource.from_definition(BUILTIN_CATALOG.sources[0])
    store = ManagedStateStore(tmp_path / "state.json")
    with pytest.raises(ManagedStateError, match="conflicts with built-in"):
        store.save(ManagedState(schema_version=8, sources=(installed,)))
    assert not store.path.exists()


def _rich_state():
    source = _installed(enabled=False)
    original = _state()
    playlist = replace(
        original.playlists[0], source_ids=(*original.playlists[0].source_ids, source.id)
    )
    draft = _draft(default_source_ids=(source.id, _source().id), cover_id="uploaded_" + "a" * 64)
    return replace(
        original,
        schema_version=8,
        sources=(*original.sources, source),
        playlists=(playlist,),
        source_overrides=(ManagedSourceOverride(SourceId("rne"), PublicReleaseMode.DISABLED),),
        templates=(draft,),
        quarantined_templates=(
            QuarantinedPlaylistTemplate(4, "invalid_definition", json.dumps({"id": "damaged"})),
        ),
    )


def test_references_drafts_overrides_quarantine_and_pending_journal_survive(tmp_path):
    store = ManagedStateStore(tmp_path / "state.json")
    state = _rich_state()
    service = ManagedAdminService(store)
    service.provisioning_journal.save(
        ProvisioningIntent(
            state=ProvisioningState.REQUEST_STARTED,
            template_id=state.templates[0].id,
            display_name="Pending",
            description="Keep",
            cover_id=state.templates[0].cover_id,
            source_ids=state.templates[0].default_source_ids,
            retention_hours=72,
            max_episodes=50,
            min_duration_seconds=30,
            max_duration_seconds=900,
        )
    )
    journal_before = service.provisioning_journal.path.read_bytes()
    store.save(state)
    assert store.load() == state
    config = compile_engine_config(BUILTIN_CATALOG, store.load())
    assert state.sources[1].id in store.load().playlists[0].source_ids
    assert state.sources[1].id not in config.playlists[0].source_selection.explicit
    assert config.playlists[0].destination == state.playlists[0].destination
    assert service.provisioning_journal.path.read_bytes() == journal_before
    before = store.path.read_bytes()
    assert parse_sources(export_source_configuration(store, ConfigurationSynchronization()))
    assert store.path.read_bytes() == before
    assert service.provisioning_journal.path.read_bytes() == journal_before


def _service(store):
    return SourceAdminService(store, ConfigurationSynchronization(), None, None, None)


def test_metadata_edit_and_disable_preserve_full_installed_definition(tmp_path):
    store = ManagedStateStore(tmp_path / "state.json")
    state = _rich_state()
    store.save(state)
    source = state.sources[1]
    service = _service(store)
    updated, _ = service.update_metadata(
        source.id,
        display_name="Revised",
        origin_kind=source.origin.kind,
        origin_code=source.origin.code,
        language="es-MX",
        editorial_scope=source.editorial_scope,
    )
    assert updated == replace(
        source,
        display_name="Revised",
        language=LanguageTag("es-MX"),
        languages=(LanguageTag("es-MX"), *source.languages[1:]),
    )
    assert store.load() == replace(state, sources=(state.sources[0], updated))
    enabled, _ = service.set_enabled(source.id, True)
    assert enabled == replace(updated, enabled=True)
    assert store.load().schema_version == 8
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        service.update_metadata(
            source.id,
            display_name="Revised",
            origin_kind=source.origin.kind,
            origin_code=source.origin.code,
            language="en",
            editorial_scope=source.editorial_scope,
        )
    assert store.path.read_bytes() == before  # would duplicate an additional language


def test_creating_draft_does_not_downgrade_v8(tmp_path):
    store = ManagedStateStore(tmp_path / "state.json")
    state = ManagedState(schema_version=8, sources=(_installed(),))
    store.save(state)
    service = ManagedAdminService(store)
    draft = service.create_local_template(
        display_name="New",
        description="Draft",
        cover_id="custom_default",
        source_ids=(state.sources[0].id,),
        retention_hours=48,
        max_episodes=20,
        min_duration_minutes=0,
        max_duration_minutes=15,
    )
    assert store.load() == replace(state, templates=(draft,))


@pytest.mark.parametrize(
    "parser,filter_id",
    [
        (s.parser_id, s.collection_filter_id)
        for s in BUILTIN_CATALOG.sources
        if not custom_strategy_allowed(s.parser_id, s.collection_filter_id)
    ],
)
def test_manual_intake_remains_restricted(tmp_path, parser, filter_id):
    store = ManagedStateStore(tmp_path / "state.json")
    service = _service(store)
    with pytest.raises(ValueError):
        service.validate_new_source(
            show_id="Y" * 22,
            display_name="Manual",
            origin_kind="country",
            origin_code="ES",
            language="es-ES",
            editorial_scope="NAT",
            timezone="Europe/Madrid",
            parser_id=parser,
            collection_filter_id=filter_id,
        )
    with pytest.raises(ValueError, match="manual creation"):
        service.create(replace(_source(), parser_id=parser, collection_filter_id=filter_id))
    assert not store.path.exists()


def test_manual_create_rejects_installed_representation_even_for_generic_parser(tmp_path):
    service = _service(ManagedStateStore(tmp_path / "state.json"))
    installed = InstalledSource.from_definition(source_definition(_source()))
    with pytest.raises(ValueError, match="manual creation"):
        service.create(installed)
    assert not service.store.path.exists()


def test_atomic_save_failure_preserves_old_state_and_cleans_temp(tmp_path, monkeypatch):
    store = ManagedStateStore(tmp_path / "state.json")
    original = _state()
    store.save(original)
    before = store.path.read_bytes()

    def interrupted(*args, **kwargs):
        raise OSError("Simulated interruption")

    monkeypatch.setattr(managed_state.os, "replace", interrupted)
    with pytest.raises(OSError):
        store.save(replace(original, schema_version=8, sources=(*original.sources, _installed())))
    assert store.path.read_bytes() == before
    assert store.load_readonly() == original
    assert list(tmp_path.iterdir()) == [store.path]


@pytest.mark.parametrize(
    "source_id,title,expected",
    [
        (
            "ser",
            "Las noticias de la SER, 12:00 (25/09/2026)",
            datetime(2026, 9, 25, 10, tzinfo=UTC),
        ),
        ("rne", "NOTICIAS RNE – 25.09.2026 – 09.30H", datetime(2026, 9, 25, 7, 30, tzinfo=UTC)),
        ("cnn", "CNN 5 cosas 09/25/2026 6:00 am", datetime(2026, 9, 25, 10, tzinfo=UTC)),
        ("rfi_fr", "Journal 25/09/2026 12h00 GMT", datetime(2026, 9, 25, 12, tzinfo=UTC)),
        ("rmf_fakty", "16:00 Fakty", datetime(2026, 9, 25, 14, tzinfo=UTC)),
        ("un_news_es", "La ONU en Minutos 25 de septiembre de 2026", None),
        (None, "Daily news", None),
    ],
)
def test_persisted_sources_keep_real_chronology_and_filtering(tmp_path, source_id, title, expected):
    definition = (
        source_definition(_source())
        if source_id is None
        else next(s for s in BUILTIN_CATALOG.sources if s.id == source_id)
    )
    store = ManagedStateStore(tmp_path / "state.json", catalog=EMPTY)
    store.save(
        ManagedState(schema_version=8, sources=(InstalledSource.from_definition(definition),))
    )
    (restored,) = effective_sources(EMPTY, store.load())
    now = datetime(2026, 9, 25, 17, tzinfo=UTC)

    class Catalogue:
        def show_episodes(self, show_id, **kwargs):
            assert show_id == definition.external_references[0].external_id
            titles = [title, "Entrevista especial"] if source_id == "un_news_es" else [title]
            return catalogue_page(
                [
                    {
                        "id": str(i) * 22,
                        "uri": "spotify:episode:" + str(i) * 22,
                        "name": name,
                        "release_date": "2026-09-25",
                        "release_date_precision": "day",
                        "duration_ms": 60000,
                    }
                    for i, name in enumerate(titles)
                ]
            )

    before = fetch_source_catalogue(
        Catalogue(), definition, now=now, cutoff=now - timedelta(days=2)
    )
    after = fetch_source_catalogue(Catalogue(), restored, now=now, cutoff=now - timedelta(days=2))
    assert before == after
    assert len(after.episodes) == 1
    if expected is not None:
        assert after.episodes[0].edition_at == expected
        assert after.episodes[0].timestamp_source is TimestampSource.TITLE
    else:
        assert after.episodes[0].timestamp_source is TimestampSource.FIRST_SEEN
    if source_id == "un_news_es":
        assert after.filter_rejected_count == 1


def test_v8_empty_envelope_is_stable_and_readonly(tmp_path, monkeypatch):
    store = ManagedStateStore(tmp_path / "state.json")
    state = ManagedState(schema_version=8)
    store.save(state)
    assert json.loads(store.path.read_bytes())["installed_sources"] is None
    before = store.path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Loading v8 must not save or migrate")

    monkeypatch.setattr(store, "save", forbidden)
    assert store.load() == state
    assert store.load_readonly() == state
    assert store.path.read_bytes() == before


def test_failure_after_atomic_replace_is_recovered_by_readback(tmp_path, monkeypatch):
    store = ManagedStateStore(tmp_path / "state.json")
    original = _state()
    store.save(original)
    updated = replace(original, schema_version=8, sources=(*original.sources, _installed()))

    def interrupted(*args, **kwargs):
        raise OSError("Simulated directory fsync failure")

    monkeypatch.setattr(managed_state, "fsync_directory", interrupted)
    with pytest.raises(OSError):
        store.save(updated)
    # Replacement has happened: retry must inspect the committed snapshot, not
    # claim that the earlier state was restored after an ambiguous durability error.
    assert store.load_readonly() == updated
    assert list(tmp_path.iterdir()) == [store.path]


def test_removed_registry_capability_cannot_be_loaded_or_silently_replaced(tmp_path, monkeypatch):
    from news_bulletin_playlist.registry import TITLE_PARSERS

    store = ManagedStateStore(tmp_path / "state.json")
    store.save(ManagedState(schema_version=8, sources=(_installed(),)))
    before = store.path.read_bytes()
    monkeypatch.delitem(TITLE_PARSERS, "ser")
    with pytest.raises(ManagedStateError, match="Invalid installed source"):
        store.load_readonly()
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"parser_id": ParserId("unknown")},
        {"collection_filter_id": "unknown"},
        {"countries": (CountryCode("ES"), CountryCode("ES"))},
        {"languages": ()},
        {"language": LanguageTag("fr")},
        {"enabled": "false"},
        {"timezone": "../secret"},
    ],
)
def test_invalid_installed_candidate_never_replaces_existing_state(tmp_path, changes):
    store = ManagedStateStore(tmp_path / "state.json")
    original = _state()
    store.save(original)
    before = store.path.read_bytes()
    bad = replace(_installed(), **changes)
    with pytest.raises(ManagedStateError):
        store.save(replace(original, schema_version=8, sources=(*original.sources, bad)))
    assert store.path.read_bytes() == before
    assert store.load() == original
