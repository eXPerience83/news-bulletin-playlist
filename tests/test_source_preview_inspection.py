"""Only well-formed unsupported capabilities become inert preview diagnostics."""

from __future__ import annotations

import copy
import json
import socket
import sqlite3
from dataclasses import replace

import pytest
from test_managed_sources import _source, _state
from test_source_configuration_export import _files
from test_source_import import _pending, _service

from news_bulletin_playlist import portable_sources
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_state import source_definition
from news_bulletin_playlist.models import ParserId, SourceId
from news_bulletin_playlist.portable_sources import (
    PortableSource,
    PortableSourceError,
    UnsupportedPortableSource,
    UnsupportedSourceCapability,
    export_sources,
    inspect_sources,
    parse_sources,
    serialize_sources,
)
from news_bulletin_playlist.registry import COLLECTION_FILTERS, TITLE_PARSERS
from news_bulletin_playlist.spotify.auth import SpotifyAuthService
from news_bulletin_playlist.spotify.client import SpotifyClient


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Preview inspection must not access network, OAuth, Spotify or SQLite")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(SpotifyAuthService, "get_access_token", forbidden)
    monkeypatch.setattr(SpotifyClient, "__init__", forbidden)


def _payload():
    return json.loads(export_sources((BUILTIN_CATALOG.sources[0],)))


def _bytes(payload):
    return json.dumps(payload).encode("utf-8")


@pytest.mark.parametrize("definition", BUILTIN_CATALOG.sources, ids=lambda source: source.id)
def test_compatible_inspection_matches_strict_decoder_and_exact_export(definition):
    encoded = export_sources((definition,), built_in_ids={definition.id})
    assert inspect_sources(encoded) == parse_sources(encoded)
    assert serialize_sources(inspect_sources(encoded)) == encoded


@pytest.mark.parametrize(
    "key,category",
    [
        ("parser", "Parser"),
        ("temporal_strategy", "Temporal strategy"),
        ("public_release_mode", "Public release policy"),
        ("collection_filter", "Collection filter"),
    ],
)
@pytest.mark.parametrize(
    "change,reason",
    [
        ("id", "Unknown capability"),
        ("version", "Unsupported version"),
        ("parameters", "Unsupported parameters"),
    ],
)
def test_well_formed_incompatibility_is_inert_and_strict_parse_still_rejects(
    monkeypatch, key, category, change, reason
):
    payload = _payload()
    processing = payload["sources"][0]["processing"]
    if key == "collection_filter":
        processing[key] = {"id": "un_news_es_minutes", "version": 1, "parameters": {}}
    processing[key][change] = {
        "id": "<script>untrusted</script>",
        "version": 2,
        "parameters": {"unsafe-value": "<script>not executed</script>"},
    }[change]
    document = _bytes(payload)
    with pytest.raises(UnsupportedSourceCapability):
        parse_sources(document)

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported rows must never construct an executable SourceDefinition")

    monkeypatch.setattr(portable_sources, "SourceDefinition", forbidden)
    (row,) = inspect_sources(document)
    assert isinstance(row, UnsupportedPortableSource)
    assert (row.id, row.display_name, row.spotify_show_id) == (
        payload["sources"][0]["original_id"],
        payload["sources"][0]["display_name"],
        payload["sources"][0]["spotify_show_id"],
    )
    assert [(issue.category, issue.reason) for issue in row.issues] == [(category, reason)]
    assert "script" not in str(row.issues) and not hasattr(row, "source")


@pytest.mark.parametrize("category", ["parser", "collection_filter"])
def test_missing_registry_entry_is_diagnosed_and_all_strict_codecs_stay_closed(
    monkeypatch, category
):
    original = BUILTIN_CATALOG.sources[0]
    if category == "collection_filter":
        original = replace(original, collection_filter_id="un_news_es_minutes")
    document = export_sources((original,))
    registry = TITLE_PARSERS if category == "parser" else COLLECTION_FILTERS
    identifier = original.parser_id if category == "parser" else original.collection_filter_id
    monkeypatch.delitem(registry, identifier)
    (row,) = inspect_sources(document)
    assert isinstance(row, UnsupportedPortableSource)
    assert row.issues[0].reason == "Capability not installed"
    assert row.issues[0].category == ("Parser" if category == "parser" else "Collection filter")
    for operation in (
        lambda: parse_sources(document),
        lambda: export_sources((original,)),
        lambda: serialize_sources((PortableSource(original, "custom"),)),
    ):
        with pytest.raises(UnsupportedSourceCapability):
            operation()


def test_unknown_parser_is_still_rejected_by_export_and_serialize():
    original = replace(BUILTIN_CATALOG.sources[0], parser_id=ParserId("unknown"))
    with pytest.raises(UnsupportedSourceCapability):
        export_sources((original,))
    with pytest.raises(UnsupportedSourceCapability):
        serialize_sources((PortableSource(original, "custom"),))


def test_all_incompatible_categories_are_reported_without_raw_capability_values():
    payload = _payload()
    processing = payload["sources"][0]["processing"]
    processing["parser"]["id"] = "unknown-secret-parser"
    processing["temporal_strategy"]["version"] = 2
    processing["public_release_mode"]["parameters"] = {"private-sentinel": "value"}
    processing["collection_filter"] = {
        "id": "unknown-secret-filter",
        "version": 1,
        "parameters": {},
    }
    (row,) = inspect_sources(_bytes(payload))
    assert isinstance(row, UnsupportedPortableSource)
    assert [issue.category for issue in row.issues] == [
        "Parser",
        "Temporal strategy",
        "Public release policy",
        "Collection filter",
    ]
    assert "secret" not in str(row) and "private-sentinel" not in str(row)


@pytest.mark.parametrize(
    "field,value",
    [
        ("original_id", "../unsafe"),
        ("spotify_show_id", "bad"),
        ("display_name", "bad\nname"),
        ("provenance", "trusted"),
        ("enabled", 1),
        ("origin", {"kind": "country", "code": "UN"}),
        ("countries", ["es"]),
        ("countries", ["ES", "ES"]),
        ("languages", []),
        ("languages", ["ES"]),
        ("timezone", "../secret"),
        ("timezone", "/etc/passwd"),
        ("editorial_scope", "UNKNOWN"),
    ],
)
def test_unsupported_capability_does_not_hide_invalid_metadata(field, value):
    payload = _payload()
    payload["sources"][0]["processing"]["parser"]["id"] = "unknown"
    payload["sources"][0][field] = value
    with pytest.raises(PortableSourceError) as error:
        inspect_sources(_bytes(payload))
    assert not isinstance(error.value, UnsupportedSourceCapability)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "ser",
        {"id": "auto", "version": True, "parameters": {}},
        {"id": "auto", "version": 0, "parameters": {}},
        {"id": "auto", "version": -1, "parameters": {}},
        {"id": "auto", "version": "2", "parameters": {}},
        {"id": "auto", "version": 1, "parameters": []},
        {"id": "", "version": 1, "parameters": {}},
        {"id": "auto", "version": 1, "parameters": {}, "extra": True},
    ],
)
def test_unsupported_first_capability_does_not_hide_later_malformed_capability(bad):
    payload = _payload()
    payload["sources"][0]["processing"]["parser"]["id"] = "unknown"
    payload["sources"][0]["processing"]["temporal_strategy"] = bad
    with pytest.raises(PortableSourceError) as error:
        inspect_sources(_bytes(payload))
    assert not isinstance(error.value, UnsupportedSourceCapability)


@pytest.mark.parametrize("missing", ["original_id", "languages", "processing"])
def test_missing_source_fields_are_not_unsupported(missing):
    payload = _payload()
    payload["sources"][0]["processing"]["parser"]["id"] = "unknown"
    payload["sources"][0].pop(missing)
    with pytest.raises(PortableSourceError):
        inspect_sources(_bytes(payload))


@pytest.mark.parametrize(
    "missing", ["parser", "temporal_strategy", "public_release_mode", "collection_filter"]
)
def test_missing_processing_fields_are_not_unsupported(missing):
    payload = _payload()
    payload["sources"][0]["processing"]["parser"]["id"] = "unknown"
    payload["sources"][0]["processing"].pop(missing)
    with pytest.raises(PortableSourceError):
        inspect_sources(_bytes(payload))


@pytest.mark.parametrize("duplicate", ["original_id", "spotify_show_id"])
@pytest.mark.parametrize("both_unsupported", [False, True])
def test_duplicates_with_unsupported_rows_reject_the_entire_batch(duplicate, both_unsupported):
    payload = _payload()
    second = copy.deepcopy(payload["sources"][0])
    payload["sources"][0]["processing"]["parser"]["id"] = "unknown"
    second["original_id"] = "second"
    second["spotify_show_id"] = "B" * 22
    second[duplicate] = payload["sources"][0][duplicate]
    if both_unsupported:
        second["processing"]["parser"]["version"] = 2
    payload["sources"].append(second)
    with pytest.raises(PortableSourceError, match="Duplicate"):
        inspect_sources(_bytes(payload))


@pytest.mark.parametrize(
    "bad",
    [
        "truncated",
        "size",
        "version",
        "format",
        "extra_root",
        "empty",
        "count",
        "duplicate_key",
        "nonfinite",
        "utf8",
    ],
)
def test_root_json_and_bounds_remain_strict(bad):
    payload = _payload()
    payload["sources"][0]["processing"]["parser"]["id"] = "unknown"
    document = _bytes(payload)
    if bad == "truncated":
        document = document[:-2]
    elif bad == "size":
        document = b" " * (256 * 1024 + 1)
    elif bad == "version":
        payload["version"] = 2
    elif bad == "format":
        payload["format"] = "other"
    elif bad == "extra_root":
        payload["unknown"] = "secret-sentinel"
    elif bad == "empty":
        payload["sources"] = []
    elif bad == "count":
        payload["sources"] *= 101
    elif bad == "duplicate_key":
        document = document.replace(b'"version": 1', b'"version": 1, "version": 1', 1)
    elif bad == "nonfinite":
        payload["sources"][0]["processing"]["parser"]["parameters"] = {"value": float("nan")}
    else:
        document = b"\xff"
    if bad not in {"truncated", "size", "duplicate_key", "utf8"}:
        document = _bytes(payload)
    with pytest.raises(PortableSourceError) as error:
        inspect_sources(document)
    assert "secret-sentinel" not in str(error.value)


def _mixed_document():
    records = [
        json.loads(export_sources((source_definition(_source("B" * 22)),)))["sources"][0],
        json.loads(
            export_sources((replace(BUILTIN_CATALOG.sources[0], id=SourceId("foreign_ser")),))
        )["sources"][0],
        json.loads(export_sources((source_definition(_source()),)))["sources"][0],
        json.loads(export_sources((replace(BUILTIN_CATALOG.sources[1], display_name="Conflict"),)))[
            "sources"
        ][0],
        json.loads(export_sources((source_definition(_source("C" * 22)),)))["sources"][0],
    ]
    records[-1]["processing"]["parser"]["id"] = "unknown"
    payload = _payload()
    payload["sources"] = records
    return _bytes(payload)


def test_mixed_preview_keeps_all_states_and_blocks_every_write(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.store.save(_state())
    _pending(service)
    before = _files(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Blocked batch must not save, migrate or prepare candidates")

    monkeypatch.setattr(service.store, "save", forbidden)
    monkeypatch.setattr(service.store, "load", forbidden)
    monkeypatch.setattr(service, "build_candidate", forbidden)
    document = _mixed_document()
    preview = service.preview(document)
    assert {row.status for row in preview.rows} == {
        "create",
        "reuse",
        "identical",
        "conflict",
        "unsupported",
    }
    assert preview.unsupported == 1 and preview.creates == 1
    assert [row.spotify_show_id for row in preview.rows] == sorted(
        row.spotify_show_id for row in preview.rows
    )
    unsupported = next(row for row in preview.rows if row.status == "unsupported")
    assert unsupported.local_id is None and isinstance(
        unsupported.incoming, UnsupportedPortableSource
    )
    assert _files(tmp_path) == before
    monkeypatch.setattr(service.store, "load_readonly", forbidden)
    for keep_local in (preview.conflicts, frozenset(range(len(preview.rows)))):
        with pytest.raises(UnsupportedSourceCapability):
            service.apply(document, preview.fingerprint, keep_local=keep_local, confirm_v8=True)
    assert _files(tmp_path) == before


def test_unsupported_existing_show_cannot_be_bypassed_by_keep_local(tmp_path, monkeypatch):
    service = _service(tmp_path)
    payload = _payload()
    payload["sources"][0]["processing"]["parser"]["version"] = 2
    document = _bytes(payload)
    preview = service.preview(document)
    assert preview.rows[0].status == "unsupported"
    assert not preview.conflicts and preview.rows[0].local_id is None

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported input must fail before state access")

    monkeypatch.setattr(service.store, "load_readonly", forbidden)
    monkeypatch.setattr(service.store, "save", forbidden)
    with pytest.raises(UnsupportedSourceCapability):
        service.apply(document, preview.fingerprint, keep_local=frozenset({0}), confirm_v8=True)
    assert not list(tmp_path.iterdir())
