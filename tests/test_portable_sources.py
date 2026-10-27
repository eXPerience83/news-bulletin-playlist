from __future__ import annotations

import json
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.custom_source_policy import custom_strategy_allowed
from news_bulletin_playlist.managed_state import (
    ManagedSource,
    ManagedSourceOverride,
    ManagedState,
    ManagedStateStore,
    effective_sources,
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
from news_bulletin_playlist.portable_sources import (
    MAX_PORTABLE_SOURCES,
    MAX_SOURCE_DOCUMENT_BYTES,
    PortableSourceError,
    UnsupportedSourceCapability,
    export_sources,
    parse_sources,
    serialize_sources,
)
from news_bulletin_playlist.registry import TITLE_PARSERS
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.first_models import TimestampSource


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Source interchange must not access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _document():
    return json.loads(export_sources((BUILTIN_CATALOG.sources[0],), built_in_ids={SourceId("ser")}))


def _decode(payload):
    return parse_sources(json.dumps(payload).encode("utf-8"))


def _custom(*, enabled=False):
    return ManagedSource(
        id=SourceId("custom_" + "A" * 22),
        spotify_show_id="A" * 22,
        display_name="Noticias personalizadas",
        origin=SourceOrigin.country(CountryCode("ES")),
        language=LanguageTag("es-419"),
        editorial_scope=EditorialScope.MIXED,
        timezone="Europe/Madrid",
        parser_id=ParserId("release_date_title"),
        collection_filter_id=None,
        enabled=enabled,
        temporal_strategy=TemporalStrategy.DISABLED,
        public_release_mode=PublicReleaseMode.WALL_TIME,
    )


@pytest.mark.parametrize("source", BUILTIN_CATALOG.sources, ids=lambda source: str(source.id))
def test_every_builtin_definition_round_trips_exactly(source):
    encoded = export_sources((source,), built_in_ids={source.id})
    (decoded,) = parse_sources(encoded)
    assert decoded.source == source
    assert decoded.provenance == "built_in"
    assert serialize_sources((decoded,)) == encoded


@pytest.mark.parametrize("strategy", list(TemporalStrategy))
@pytest.mark.parametrize("mode", list(PublicReleaseMode))
@pytest.mark.parametrize("enabled", [True, False])
def test_custom_round_trip_all_temporal_modes_and_disabled_state(strategy, mode, enabled):
    managed = replace(
        _custom(enabled=enabled), temporal_strategy=strategy, public_release_mode=mode
    )
    (source,) = effective_sources(BuiltInCatalog((), ()), ManagedState(sources=(managed,)))
    (decoded,) = parse_sources(export_sources((source,)))
    assert decoded.source == source
    assert decoded.provenance == "custom"


def test_effective_overrides_batch_determinism_and_repeat_decode():
    state = ManagedState(
        sources=(_custom(),),
        source_overrides=(ManagedSourceOverride(SourceId("rne"), PublicReleaseMode.DISABLED),),
    )
    effective = effective_sources(BUILTIN_CATALOG, state)
    builtin_ids = {source.id for source in BUILTIN_CATALOG.sources}
    encoded = export_sources(effective, built_in_ids=builtin_ids)
    assert encoded == export_sources(tuple(reversed(effective)), built_in_ids=builtin_ids)
    parsed = parse_sources(encoded)
    assert parsed == parse_sources(encoded)
    assert encoded == serialize_sources(parsed)
    assert {item.source.id: item.source for item in parsed} == {s.id: s for s in effective}
    rne = next(item.source for item in parsed if item.source.id == "rne")
    assert rne.public_release_mode is PublicReleaseMode.DISABLED


def test_multiple_classifications_are_not_collapsed_to_custom_source_shape():
    source = replace(
        BUILTIN_CATALOG.sources[0],
        countries=(CountryCode("ES"), CountryCode("GB")),
        languages=(LanguageTag("es-ES"), LanguageTag("en")),
    )
    assert parse_sources(export_sources((source,)))[0].source == source


def test_different_logical_id_keeps_the_same_canonical_show():
    source = BUILTIN_CATALOG.sources[0]
    renamed = replace(source, id=SourceId("another_installation_id"))
    (decoded,) = parse_sources(export_sources((renamed,)))
    assert decoded.source.id == renamed.id
    assert decoded.source.external_references == source.external_references
    with pytest.raises(PortableSourceError, match="Duplicate"):
        export_sources((source, renamed))


@pytest.mark.parametrize("conflict", [False, True])
def test_repeated_show_in_batch_rejected_even_if_metadata_differs(conflict):
    payload = _document()
    other = dict(payload["sources"][0], original_id="alias")
    if conflict:
        other["display_name"] = "Different metadata"
    payload["sources"].append(other)
    with pytest.raises(PortableSourceError, match="Duplicate"):
        _decode(payload)


def test_duplicate_logical_id_with_different_show_rejected():
    payload = _document()
    payload["sources"].append(dict(payload["sources"][0], spotify_show_id="B" * 22))
    with pytest.raises(PortableSourceError, match="Duplicate"):
        _decode(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", 1),
        ("enabled", "false"),
        ("original_id", "../escape"),
        ("spotify_show_id", "spotify:show:" + "A" * 22),
        ("spotify_show_id", "x"),
        ("display_name", " "),
        ("display_name", "x" * 121),
        ("display_name", "bad\ntext"),
        ("display_name", "\ud800"),
        ("countries", ["es"]),
        ("countries", ["ES", "ES"]),
        ("countries", "ES"),
        ("languages", []),
        ("languages", ["es", "es"]),
        ("languages", ["en_US"]),
        ("languages", [False]),
        ("languages", ["es"] * 11),
        ("timezone", "Invalid/Zone"),
        ("timezone", "/etc/passwd"),
        ("timezone", "../UTC"),
        ("editorial_scope", "unknown"),
        ("origin", {"kind": "country", "code": "UN"}),
        ("provenance", "trusted"),
    ],
)
def test_invalid_source_fields_rejected(field, value):
    payload = _document()
    payload["sources"][0][field] = value
    with pytest.raises(PortableSourceError):
        _decode(payload)


@pytest.mark.parametrize("target", ["root", "source", "origin", "processing", "parser"])
@pytest.mark.parametrize("action", ["extra", "missing"])
def test_exact_fields_required_at_every_level(target, action):
    payload = _document()
    source = payload["sources"][0]
    selected = {
        "root": payload,
        "source": source,
        "origin": source["origin"],
        "processing": source["processing"],
        "parser": source["processing"]["parser"],
    }[target]
    if action == "extra":
        selected["access_token"] = "secret-sentinel"
    else:
        selected.pop(next(iter(selected)))
    with pytest.raises(PortableSourceError) as error:
        _decode(payload)
    assert "secret-sentinel" not in str(error.value)
    assert "access_token" not in str(error.value)


@pytest.mark.parametrize(
    "capability", ["parser", "temporal_strategy", "public_release_mode", "collection_filter"]
)
@pytest.mark.parametrize(
    "field,value", [("id", "unknown"), ("version", 2), ("parameters", {"regex": ".*"})]
)
def test_unknown_capability_contracts_never_degrade(capability, field, value):
    payload = _document()
    if capability == "collection_filter":
        payload["sources"][0]["processing"][capability] = {
            "id": "un_news_es_minutes",
            "version": 1,
            "parameters": {},
        }
    payload["sources"][0]["processing"][capability][field] = value
    with pytest.raises(UnsupportedSourceCapability):
        _decode(payload)


@pytest.mark.parametrize("value", [True, 1.0, "1", 0, -1, None])
def test_capability_versions_are_positive_integers(value):
    payload = _document()
    payload["sources"][0]["processing"]["parser"]["version"] = value
    with pytest.raises(PortableSourceError):
        _decode(payload)


def test_capability_must_also_be_installed(monkeypatch):
    document = export_sources((BUILTIN_CATALOG.sources[0],))
    monkeypatch.delitem(TITLE_PARSERS, "ser")
    with pytest.raises(UnsupportedSourceCapability):
        parse_sources(document)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{",
        b"\xff",
        b"[]",
        b"null",
        b'{"version":1,"version":1}',
        b'{"secret-sentinel":NaN}',
        b"[[[[" * 1000 + b"]]]]" * 1000,
        b" " * (MAX_SOURCE_DOCUMENT_BYTES + 1),
    ],
)
def test_corrupt_or_unbounded_json_has_safe_errors(payload):
    with pytest.raises(PortableSourceError) as error:
        parse_sources(payload)
    assert "secret-sentinel" not in str(error.value)


def test_duplicate_nested_json_key_rejected():
    encoded = export_sources((BUILTIN_CATALOG.sources[0],))
    encoded = encoded.replace(b'"enabled": true', b'"enabled": true, "enabled": false')
    with pytest.raises(PortableSourceError):
        parse_sources(encoded)


@pytest.mark.parametrize("version", [0, 2, True, 1.0, "1", None])
def test_unsupported_document_version(version):
    payload = _document()
    payload["version"] = version
    with pytest.raises(PortableSourceError, match="version"):
        _decode(payload)


def test_counts_and_non_source_formats_are_rejected():
    payload = _document()
    payload["format"] = "managed-state"
    with pytest.raises(PortableSourceError, match="format"):
        _decode(payload)
    for count in (0, MAX_PORTABLE_SOURCES + 1):
        with pytest.raises(PortableSourceError):
            export_sources((BUILTIN_CATALOG.sources[0],) * count)
        payload = _document()
        payload["sources"] *= count
        with pytest.raises(PortableSourceError):
            _decode(payload)


def test_extra_external_references_not_silently_dropped_or_exposed():
    source = BUILTIN_CATALOG.sources[0]
    extra = ExternalReference("private", "credential", "secret-sentinel")
    with pytest.raises(PortableSourceError) as error:
        export_sources((replace(source, external_references=(*source.external_references, extra)),))
    assert "secret-sentinel" not in str(error.value)


def test_no_installation_writes_or_custom_policy_expansion(tmp_path: Path, monkeypatch):
    state = ManagedState(sources=(_custom(),))
    (source,) = effective_sources(BuiltInCatalog((), ()), state)
    sentinel = tmp_path / "managed-state.json"
    sentinel.write_bytes(b"existing-installation-state")

    def forbidden(*args, **kwargs):
        pytest.fail("Interchange must not load or save installation state")

    monkeypatch.setattr(ManagedStateStore, "load", forbidden)
    monkeypatch.setattr(ManagedStateStore, "save", forbidden)
    assert parse_sources(export_sources((source,)))[0].source == source
    assert sentinel.read_bytes() == b"existing-installation-state"
    assert list(tmp_path.iterdir()) == [sentinel]
    assert not custom_strategy_allowed("ser", None)
    assert not custom_strategy_allowed("release_date_title", "un_news_es_minutes")


@pytest.mark.parametrize(
    "source_id,titles",
    [
        ("ser", ["Las noticias de la SER, 12:00 (25/09/2026)"]),
        ("rne", ["NOTICIAS RNE – 25.09.2026 – 09.30H"]),
        ("un_news_es", ["La ONU en Minutos 25 de septiembre de 2026", "Entrevista especial"]),
    ],
)
def test_round_trip_preserves_runtime_chronology_and_editorial_selection(source_id, titles):
    source = next(s for s in BUILTIN_CATALOG.sources if s.id == source_id)
    decoded = parse_sources(export_sources((source,)))[0].source
    now = datetime(2026, 9, 25, 17, tzinfo=UTC)

    class Catalogue:
        def show_episodes(self, show_id, **kwargs):
            assert show_id == source.external_references[0].external_id
            return catalogue_page(
                [
                    {
                        "id": str(index) * 22,
                        "uri": "spotify:episode:" + str(index) * 22,
                        "name": title,
                        "release_date": "2026-09-25",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    }
                    for index, title in enumerate(titles)
                ]
            )

    before = fetch_source_catalogue(Catalogue(), source, now=now, cutoff=now - timedelta(days=2))
    after = fetch_source_catalogue(Catalogue(), decoded, now=now, cutoff=now - timedelta(days=2))
    assert before == after
    assert len(after.episodes) == 1
    if source_id == "un_news_es":
        assert after.filter_rejected_count == 1
    else:
        assert after.episodes[0].timestamp_source is TimestampSource.TITLE
