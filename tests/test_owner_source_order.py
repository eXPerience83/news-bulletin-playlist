"""Exact historical sources and order survive full installed-record serialization.

This is pure v11 owner-layout evidence only: current live v11 still uses frozen
historical presets and must not be marked owner-editable by these tests alone.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from dataclasses import replace

import pytest
from _installed_fixtures import _installed
from _legacy_materialization_fixtures import _legacy_state
from _managed_fixtures import _source

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.managed_state import (
    ManagedState,
    ManagedStateStore,
    compile_engine_config,
    source_definition,
)
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.owner_source_order import (
    OwnerSourceOrder,
    OwnerSourceOrderError,
    materialize_owner_sources,
)

EMPTY = BuiltInCatalog((), ())


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Pure owner-source projection attempted network or operational DB I/O")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "getaddrinfo", blocked)
    monkeypatch.setattr(sqlite3, "connect", blocked)


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_old_runtime_definitions_and_order_survive_as_full_owner_records(tmp_path, schema):
    state = _legacy_state(schema, BUILTIN_CATALOG)
    old_runtime = compile_engine_config(BUILTIN_CATALOG, state).sources
    order, records = materialize_owner_sources(old_runtime)
    assert order.ids == tuple(item.id for item in old_runtime)
    assert order.definitions(records) == old_runtime
    assert len(records) == len(old_runtime) and len(records) >= len(BUILTIN_CATALOG.sources)
    assert records[0].id == BUILTIN_CATALOG.sources[0].id
    # All specialized parser/filter/temporal/public-release capabilities are
    # round-tripped by the portable InstalledSource codec, not a generic parser.
    assert tuple(record.parser_id for record in records) == tuple(
        definition.parser_id for definition in old_runtime
    )
    assert tuple(record.collection_filter_id for record in records) == tuple(
        definition.collection_filter_id for definition in old_runtime
    )
    assert tuple(record.temporal_strategy for record in records) == tuple(
        definition.temporal_strategy for definition in old_runtime
    )
    assert tuple(record.public_release_mode for record in records) == tuple(
        definition.public_release_mode for definition in old_runtime
    )

    persisted = tmp_path / "owner-managed-state.json"
    store = ManagedStateStore(persisted, catalog=EMPTY)
    # The old built-in IDs are now independent local InstalledSource records,
    # with NO built-in sources in the receiving catalog.
    store.save(ManagedState(schema_version=10, sources=records))
    loaded = store.load_readonly()
    assert {item.id for item in loaded.sources} == set(order.ids)
    assert loaded.sources == tuple(sorted(records, key=lambda item: item.id))
    evidence = order.serialize()
    assert OwnerSourceOrder.parse(evidence).definitions(loaded.sources) == old_runtime
    assert store.load_readonly() == loaded
    assert len(evidence) < 64 * 1024


def test_owner_order_preserves_edits_removals_and_appends_new_ids_deterministically():
    # A legacy manually configured source is materialized into the same
    # complete owner-record type as the historical image presets.
    runtime = BUILTIN_CATALOG.sources[:4] + (source_definition(_source()),)
    order, records = materialize_owner_sources(runtime)
    old_count = len(records)
    changed = tuple(
        replace(item, display_name="Edited owner name", enabled=False)
        if item.id == records[1].id
        else item
        for item in records
        if item.id != records[0].id
    )
    original = _installed()
    new_one = replace(original, id=SourceId("owner_alpha"), spotify_show_id="Z" * 22)
    new_two = replace(original, id=SourceId("owner_beta"), spotify_show_id="Y" * 22)
    updated_records = changed + (new_two, new_one)
    updated_order = order.after_records_change(records, updated_records)
    assert updated_order.ids == tuple(item.id for item in records[1:]) + (
        SourceId("owner_alpha"),
        SourceId("owner_beta"),
    )
    definitions = updated_order.definitions(updated_records)
    assert len(definitions) == old_count + 1
    assert definitions[0].display_name == "Edited owner name"
    assert not definitions[0].enabled
    assert definitions[0].parser_id == runtime[1].parser_id
    with pytest.raises(OwnerSourceOrderError, match="records do not match"):
        order.definitions(changed)
    with pytest.raises(OwnerSourceOrderError, match="Duplicate"):
        updated_order.after_records_change(updated_records, updated_records + (new_one,))


@pytest.mark.parametrize(
    "invalid",
    [
        b"{",
        b"",
        b" " * 65537,
        b'{"format":"news-bulletin-playlist.owner-source-order","version":1,"source_ids":["a","a"]}\n',
        b'{"format":"news-bulletin-playlist.owner-source-order","version":1,"source_ids":["bad/id"]}\n',
        b'{"format":"news-bulletin-playlist.owner-source-order","version":true,"source_ids":[]}\n',
        b'{"format":"news-bulletin-playlist.owner-source-order","version":1,"source_ids":[],"extra":1}\n',
        b'{"format":"news-bulletin-playlist.owner-source-order","version":1,"source_ids":[],"version":1}\n',
        b'{"format":"news-bulletin-playlist.owner-source-order","version":1,"source_ids":[]}',
    ],
)
def test_invalid_or_noncanonical_order_fails_closed(invalid):
    with pytest.raises(OwnerSourceOrderError):
        OwnerSourceOrder.parse(invalid)


def test_unordered_catalog_state_cannot_be_assumed_to_be_original_order(tmp_path):
    order, records = materialize_owner_sources(BUILTIN_CATALOG.sources)
    assert order.ids != tuple(sorted(order.ids))
    store = ManagedStateStore(tmp_path / "state.json", catalog=EMPTY)
    store.save(ManagedState(schema_version=10, sources=records))
    canonical_state = store.load_readonly()
    # Sorting the saved state is expected; explicit order evidence is essential.
    assert tuple(item.id for item in canonical_state.sources) != order.ids
    observed = OwnerSourceOrder.parse(order.serialize()).definitions(canonical_state.sources)
    assert observed == BUILTIN_CATALOG.sources
    malformed = json.loads(order.serialize())
    malformed["source_ids"].pop()
    invalid_evidence = (
        json.dumps(malformed, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with pytest.raises(OwnerSourceOrderError, match="records do not match"):
        OwnerSourceOrder.parse(invalid_evidence).definitions(canonical_state.sources)
