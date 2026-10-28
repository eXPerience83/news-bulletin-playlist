"""Source import is local, lossless, fail-closed and idempotent (#549)."""

from __future__ import annotations

import json
import socket
from dataclasses import replace

import pytest
from test_installed_sources import EMPTY, _installed, _rich_state
from test_managed_sources import _source, _state

from news_bulletin_playlist import managed_state
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_provisioning import (
    ProvisioningIntent,
    ProvisioningJournal,
    ProvisioningState,
)
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    effective_sources,
    source_definition,
)
from news_bulletin_playlist.models import (
    CountryCode,
    EditorialScope,
    ExternalReference,
    LanguageTag,
    ParserId,
    PublicReleaseMode,
    SourceId,
    SourceOrigin,
    TemporalStrategy,
)
from news_bulletin_playlist.portable_sources import PortableSourceError, export_sources
from news_bulletin_playlist.source_import import (
    SourceImportError,
    SourceImportService,
    SourceImportWriteError,
    StaleSourceImport,
)
from news_bulletin_playlist.spotify.auth import SpotifyAuthService
from news_bulletin_playlist.spotify.client import SpotifyClient


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Source import must not contact network/OAuth/Spotify")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(SpotifyAuthService, "get_access_token", forbidden)
    monkeypatch.setattr(SpotifyClient, "__init__", forbidden)


def _service(tmp_path, *, catalog=BUILTIN_CATALOG):
    store = ManagedStateStore(tmp_path / "managed-state.json", catalog=catalog)
    return SourceImportService(
        store, ConfigurationSynchronization(), ProvisioningJournal(tmp_path / "pending.json")
    )


def _document(**changes):
    return export_sources((replace(source_definition(_source()), **changes),))


def _apply(service, document, *, keep_local=frozenset(), confirm_v8=True):
    preview = service.preview(document)
    return service.apply(
        document, preview.fingerprint, keep_local=keep_local, confirm_v8=confirm_v8
    )


@pytest.mark.parametrize("definition", BUILTIN_CATALOG.sources, ids=lambda source: source.id)
def test_import_all_original_builtins_as_complete_installed_definitions(
    tmp_path, definition, monkeypatch
):
    service = _service(tmp_path, catalog=EMPTY)
    document = export_sources((definition,), built_in_ids={definition.id})
    preview = service.preview(document)
    assert not list(tmp_path.iterdir())
    assert preview.rows[0].status == "create"
    result = service.apply(document, preview.fingerprint, keep_local=frozenset(), confirm_v8=True)
    assert result.created == 1
    source = service.store.load_readonly().sources[0]
    assert isinstance(source, InstalledSource)
    assert source_definition(source) == replace(definition, id=result.rows[0].local_id)
    before = service.store.path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Repeated import must not save")

    monkeypatch.setattr(service.store, "save", forbidden)
    assert _apply(service, document).created == 0
    assert service.store.path.read_bytes() == before


@pytest.mark.parametrize("version", [6, 7, 8])
@pytest.mark.parametrize("foreign_id", [False, True])
def test_existing_manual_show_reuses_identity_without_any_write(
    tmp_path, version, foreign_id, monkeypatch
):
    service = _service(tmp_path)
    state = replace(_state(), schema_version=version)
    service.store.save(state)
    source = source_definition(state.sources[0])
    if foreign_id:
        source = replace(source, id=SourceId("foreign_logical_id"))
    document = export_sources((source,), built_in_ids={source.id})  # untrusted provenance ignored
    before = service.store.path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("No-op import must not load/migrate or save")

    monkeypatch.setattr(service.store, "load", forbidden)
    monkeypatch.setattr(service.store, "save", forbidden)
    preview = service.preview(document)
    assert preview.rows[0].status == ("reuse" if foreign_id else "identical")
    assert preview.rows[0].local_id == state.sources[0].id
    result = service.apply(document, preview.fingerprint, keep_local=frozenset(), confirm_v8=False)
    assert result.created == 0
    assert service.store.path.read_bytes() == before


def test_disabled_multilingual_source_and_v8_append(tmp_path):
    service = _service(tmp_path)
    state = ManagedState(schema_version=8, sources=(_installed(),))
    service.store.save(state)
    definition = replace(
        source_definition(_source(enabled=False)),
        countries=(CountryCode("AR"),),
        languages=(LanguageTag("es-419"), LanguageTag("en")),
        public_release_mode=PublicReleaseMode.WALL_TIME,
    )
    result = _apply(service, export_sources((definition,)), confirm_v8=False)
    assert result.created == 1
    stored = service.store.load_readonly()
    assert stored.schema_version == 8
    assert state.sources[0] in stored.sources
    assert definition in effective_sources(service.store.catalog, stored)


@pytest.mark.parametrize("version", [6, 7])
def test_v8_adoption_requires_explicit_acknowledgement(tmp_path, version):
    service = _service(tmp_path)
    service.store.save(ManagedState(schema_version=version))
    before = service.store.path.read_bytes()
    preview = service.preview(_document())
    assert service.store.path.read_bytes() == before
    with pytest.raises(SourceImportError, match="acknowledgement"):
        service.apply(_document(), preview.fingerprint, keep_local=frozenset(), confirm_v8=False)
    assert service.store.path.read_bytes() == before
    assert _apply(service, _document()).created == 1
    assert service.store.load_readonly().schema_version == 8


def test_original_id_collision_never_overwrites_and_allocator_handles_occupied_ids(tmp_path):
    service = _service(tmp_path)
    occupied = _installed(id=SourceId("custom_" + "A" * 22))
    second = _installed(
        id=SourceId("custom_" + "A" * 22 + "_2"),
        external_references=(ExternalReference("spotify", "show", "Y" * 22),),
    )
    service.store.save(ManagedState(schema_version=8, sources=(occupied, second)))
    document = _document(id=SourceId("ser"))  # foreign logical ID already belongs to another show
    preview = service.preview(document)
    assert preview.rows[0].local_id == SourceId("custom_" + "A" * 22 + "_3")
    assert _apply(service, document).created == 1
    assert occupied in service.store.load_readonly().sources
    assert second in service.store.load_readonly().sources
    assert _apply(service, document).rows[0].local_id == preview.rows[0].local_id


@pytest.mark.parametrize(
    "changes",
    [
        {"display_name": "Different"},
        {"countries": (CountryCode("ES"),)},
        {"languages": (LanguageTag("es"), LanguageTag("en"))},
        {"enabled": False},
        {"parser_id": ParserId("ser")},
        {"collection_filter_id": "un_news_es_minutes"},
        {"temporal_strategy": TemporalStrategy.DISABLED},
        {"public_release_mode": PublicReleaseMode.DISABLED},
        {"origin": SourceOrigin.institution("UN")},
        {"editorial_scope": EditorialScope.NATIONAL},
    ],
)
def test_each_operational_difference_is_a_conflict_and_keep_local_never_replaces(tmp_path, changes):
    service = _service(tmp_path)
    state = _state()
    service.store.save(state)
    document = _document(**changes)
    preview = service.preview(document)
    assert preview.rows[0].status == "conflict"
    assert preview.rows[0].differences
    before = service.store.path.read_bytes()
    with pytest.raises(SourceImportError, match="Every conflict"):
        service.apply(document, preview.fingerprint, keep_local=frozenset(), confirm_v8=True)
    assert service.store.path.read_bytes() == before
    assert (
        service.apply(
            document, preview.fingerprint, keep_local=frozenset({0}), confirm_v8=False
        ).created
        == 0
    )
    assert service.store.path.read_bytes() == before


def test_ordered_classifications_and_timezone_participate_in_equivalence(tmp_path):
    from zoneinfo import ZoneInfo

    service = _service(tmp_path)
    source = _installed()
    service.store.save(ManagedState(schema_version=8, sources=(source,)))
    for changed in (
        replace(source_definition(source), countries=tuple(reversed(source.countries))),
        replace(source_definition(source), languages=tuple(reversed(source.languages))),
        replace(source_definition(source), timezone=ZoneInfo("UTC")),
    ):
        assert service.preview(export_sources((changed,))).rows[0].status == "conflict"


def test_builtin_reuse_and_conflicts_observe_effective_override(tmp_path):
    from news_bulletin_playlist.managed_state import ManagedSourceOverride

    service = _service(tmp_path)
    service.store.save(
        ManagedState(
            source_overrides=(ManagedSourceOverride(SourceId("ser"), PublicReleaseMode.DISABLED),)
        )
    )
    builtin = BUILTIN_CATALOG.sources[0]
    assert service.preview(export_sources((builtin,))).rows[0].status == "conflict"
    effective = replace(
        builtin, public_release_mode=PublicReleaseMode.DISABLED, id=SourceId("foreign")
    )
    assert _apply(service, export_sources((effective,))).created == 0
    assert service.store.load_readonly().schema_version == 6


def test_mixed_batch_conflicts_block_all_additions_until_resolved(tmp_path):
    service = _service(tmp_path)
    service.store.save(_state())
    document = export_sources(
        (
            replace(source_definition(_source()), display_name="Conflict"),
            source_definition(_source("B" * 22)),
        )
    )
    preview = service.preview(document)
    before = service.store.path.read_bytes()
    with pytest.raises(SourceImportError):
        service.apply(document, preview.fingerprint, keep_local=frozenset(), confirm_v8=True)
    assert service.store.path.read_bytes() == before
    result = service.apply(
        document, preview.fingerprint, keep_local=preview.conflicts, confirm_v8=True
    )
    assert result.created == 1
    assert _source() in service.store.load_readonly().sources
    assert len(service.store.load_readonly().sources) == 2


@pytest.mark.parametrize(
    "corruption",
    ["truncated", "large", "unknown_field", "duplicate", "version", "capability", "bad_utf8"],
)
def test_invalid_document_is_rejected_before_configuration_access(
    tmp_path, corruption, monkeypatch
):
    service = _service(tmp_path)
    document = _document()
    payload = json.loads(document)
    if corruption == "truncated":
        document = document[:-5]
    elif corruption == "large":
        document = b" " * (256 * 1024 + 1)
    elif corruption == "unknown_field":
        payload["extra"] = "secret-sentinel"
    elif corruption == "duplicate":
        payload["sources"] *= 2
    elif corruption == "version":
        payload["version"] = 2
    elif corruption == "capability":
        payload["sources"][0]["processing"]["parser"]["id"] = "secret-sentinel"
    else:
        document = b"\xff"
    if corruption not in {"truncated", "large", "bad_utf8"}:
        document = json.dumps(payload).encode()

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid document must not read local configuration")

    monkeypatch.setattr(service.store, "load_readonly", forbidden)
    with pytest.raises(PortableSourceError) as error:
        service.preview(document)
    assert "secret-sentinel" not in str(error.value)
    assert not list(tmp_path.iterdir())


def _pending(service, source_ids=("ser",)):
    intent = ProvisioningIntent(
        ProvisioningState.REQUEST_STARTED,
        BUILTIN_CATALOG.playlists[0].id,
        "Pending",
        "Keep description",
        "spain_spanish_news",
        source_ids,
        retention_hours=48,
        max_episodes=50,
        max_duration_seconds=900,
    )
    service.provisioning.save(intent)
    return intent


@pytest.mark.parametrize("change", ["source", "references", "quarantine", "pending"])
def test_stale_confirmation_rejected_without_a_save(tmp_path, change, monkeypatch):
    service = _service(tmp_path)
    state = _rich_state()
    service.store.save(state)
    _pending(service)
    document = export_sources((source_definition(_source("B" * 22)),))
    preview = service.preview(document)
    if change == "source":
        service.store.save(
            replace(
                state, sources=(replace(state.sources[0], display_name="Changed"), state.sources[1])
            )
        )
    elif change == "references":
        service.store.save(
            replace(state, playlists=(replace(state.playlists[0], source_ids=("ser",)),))
        )
    elif change == "quarantine":
        service.store.save(replace(state, quarantined_templates=()))
    else:
        _pending(service, ("rne",))
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    def forbidden(*args, **kwargs):
        pytest.fail("Stale confirmation must not save")

    monkeypatch.setattr(service.store, "save", forbidden)
    with pytest.raises(StaleSourceImport):
        service.apply(document, preview.fingerprint, keep_local=frozenset(), confirm_v8=True)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_candidate_preserves_all_existing_references_and_pending_journal(tmp_path):
    service = _service(tmp_path)
    state = _rich_state()
    service.store.save(state)
    _pending(service)
    journal = service.provisioning.path.read_bytes()
    document = export_sources((source_definition(_source("B" * 22, enabled=False)),))
    assert _apply(service, document).created == 1
    result = service.store.load_readonly()
    assert replace(result, sources=state.sources) == state
    assert all(source in result.sources for source in state.sources)
    assert service.provisioning.path.read_bytes() == journal


def test_invalid_pending_references_block_additions_without_writes(tmp_path):
    service = _service(tmp_path)
    _pending(service, ("unknown",))
    before = service.provisioning.path.read_bytes()
    with pytest.raises(SourceImportError, match="Pending activation"):
        service.preview(_document())
    assert not service.store.path.exists()
    assert service.provisioning.path.read_bytes() == before


@pytest.mark.parametrize("point", ["prepare", "file_fsync", "replace", "chmod", "directory_fsync"])
def test_pre_and_post_commit_failures_have_readback_based_recovery(tmp_path, monkeypatch, point):
    service = _service(tmp_path)
    state = ManagedState()
    service.store.save(state)
    document = _document()
    preview = service.preview(document)
    before = service.store.path.read_bytes()
    calls = 0
    real_save = service.store.save

    def save(candidate):
        nonlocal calls
        calls += 1
        real_save(candidate)

    monkeypatch.setattr(service.store, "save", save)

    def fail(*args, **kwargs):
        raise OSError("Simulated storage interruption")

    with monkeypatch.context() as storage:
        if point == "prepare":
            original_open = managed_state.os.open

            def fail_temp(path, *args, **kwargs):
                if str(path).endswith(".tmp"):
                    fail()
                return original_open(path, *args, **kwargs)

            storage.setattr(managed_state.os, "open", fail_temp)
        elif point == "directory_fsync":
            storage.setattr(managed_state, "fsync_directory", fail)
        else:
            storage.setattr(
                managed_state.os,
                {"file_fsync": "fsync", "replace": "replace", "chmod": "chmod"}[point],
                fail,
            )
        if point in {"chmod", "directory_fsync"}:
            result = service.apply(
                document, preview.fingerprint, keep_local=frozenset(), confirm_v8=True
            )
            assert result.created == 1 and result.recovered
        else:
            with pytest.raises(SourceImportWriteError, match="previous configuration is intact"):
                service.apply(
                    document, preview.fingerprint, keep_local=frozenset(), confirm_v8=True
                )
            assert service.store.path.read_bytes() == before
    assert calls == 1
    assert not list(tmp_path.glob("*.tmp"))
    result = _apply(service, document)
    assert result.created == (0 if point in {"chmod", "directory_fsync"} else 1)
    assert calls == (1 if point in {"chmod", "directory_fsync"} else 2)


def test_failed_readback_does_not_claim_rollback(tmp_path, monkeypatch):
    service = _service(tmp_path)
    document = _document()
    preview = service.preview(document)
    read = service.store.load_readonly

    def unreadable():
        raise ManagedStateError("private storage failure")

    def ambiguous_save(candidate):
        monkeypatch.setattr(service.store, "load_readonly", unreadable)
        raise OSError("private-error-sentinel")

    with monkeypatch.context() as scoped:
        scoped.setattr(service.store, "save", ambiguous_save)
        with pytest.raises(SourceImportWriteError, match="uncertain") as error:
            service.apply(document, preview.fingerprint, keep_local=frozenset(), confirm_v8=True)
        assert "private-error-sentinel" not in str(error.value)
    monkeypatch.setattr(service.store, "load_readonly", read)
    assert _apply(service, document).created == 1


def test_different_installations_have_independent_mapping_and_no_shared_state(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    first = _service(a)
    second = _service(b)
    second.store.save(
        ManagedState(
            schema_version=8,
            sources=(
                InstalledSource.from_definition(
                    replace(source_definition(_source()), id=SourceId("local_alias"))
                ),
            ),
        )
    )
    document = _document()
    result_a = _apply(first, document)
    result_b = _apply(second, document)
    assert result_a.created == 1 and result_b.created == 0
    assert result_a.rows[0].local_id != result_b.rows[0].local_id
    assert _apply(first, document).rows[0].local_id == result_a.rows[0].local_id


@pytest.mark.parametrize(
    "source_id,title,expected_hour,expected_minute",
    [
        ("ser", "Las noticias de la SER, 12:00 (25/09/2026)", 10, 0),
        ("rne", "NOTICIAS RNE – 25.09.2026 – 09.30H", 7, 30),
        ("cnn", "CNN 5 cosas 09/25/2026 6:00 am", 10, 0),
        ("rfi_fr", "Journal 25/09/2026 12h00 GMT", 12, 0),
        ("rmf_fakty", "16:00 Fakty", 14, 0),
        ("un_news_es", "La ONU en Minutos 25 de septiembre de 2026", None, None),
    ],
)
def test_imported_specialized_capabilities_keep_real_episode_decisions(
    tmp_path, source_id, title, expected_hour, expected_minute
):
    from datetime import UTC, datetime, timedelta

    from _spotify_catalogue import catalogue_page

    from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
    from news_bulletin_playlist.spotify.first_models import TimestampSource

    service = _service(tmp_path, catalog=EMPTY)
    original = next(s for s in BUILTIN_CATALOG.sources if s.id == source_id)
    result = _apply(service, export_sources((original,)))
    (restored,) = effective_sources(EMPTY, service.store.load_readonly())
    expected = replace(original, id=result.rows[0].local_id)
    now = datetime(2026, 9, 25, 17, tzinfo=UTC)

    class Catalogue:
        def show_episodes(self, show_id, **kwargs):
            assert show_id == original.external_references[0].external_id
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

    before = fetch_source_catalogue(Catalogue(), expected, now=now, cutoff=now - timedelta(days=2))
    after = fetch_source_catalogue(Catalogue(), restored, now=now, cutoff=now - timedelta(days=2))
    assert before == after and len(after.episodes) == 1
    if expected_hour is not None:
        assert after.episodes[0].edition_at == datetime(
            2026, 9, 25, expected_hour, expected_minute, tzinfo=UTC
        )
        assert after.episodes[0].timestamp_source is TimestampSource.TITLE
    else:
        assert after.filter_rejected_count == 1
        assert after.episodes[0].timestamp_source is TimestampSource.FIRST_SEEN
