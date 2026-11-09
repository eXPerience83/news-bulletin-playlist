"""Owner template codec recovers historical names, IDs, policies and semantics offline."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import time

import pytest
from _legacy_materialization_fixtures import _catalog_with_duration_exception, _legacy_state

from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.legacy_materialization import (
    LegacyProvenance,
    pause_dormant_legacy_playlists,
    preview_legacy_materialization,
)
from news_bulletin_playlist.managed_state import (
    ManagedState,
    compile_engine_config,
)
from news_bulletin_playlist.models import DurationPolicy, DurationPolicyException, SourceId
from news_bulletin_playlist.owner_source_order import materialize_owner_sources
from news_bulletin_playlist.owner_template_records import (
    OwnerTemplateError,
    OwnerTemplateSet,
    materialize_owner_templates,
)


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_four_destinations_with_historical_template_policies_are_semantically_unchanged(
    schema: int,
):
    installed_catalog = _catalog_with_duration_exception()
    legacy = _legacy_state(schema, installed_catalog)
    expected = preview_legacy_materialization(installed_catalog, legacy)

    order, records = materialize_owner_sources(expected.original_runtime.sources)
    original_owner_templates = tuple(
        template.definition
        for template in expected.template_snapshots
        if template.provenance == LegacyProvenance.IMAGE_PRESET
    )
    owner = materialize_owner_templates(original_owner_templates, sources=order.ids)
    canonical = owner.serialize()
    assert OwnerTemplateSet.parse(canonical) == owner
    assert len(owner.records) == len(installed_catalog.playlists)
    assert [item.id for item in owner.records] == [
        item.id for item in installed_catalog.playlists
    ]
    assert owner.records[0].duration_policy == original_owner_templates[0].duration_policy

    # Reconstruct a "legacy-shaped" compilation solely from owner records
    # and original owner-selectable state. No dependency on the image defaults.
    owned_sources = order.definitions(records)
    preset_ids = {
        record.installed.id
        for record in expected.source_snapshots
        if record.provenance == LegacyProvenance.IMAGE_PRESET
    }
    source_catalog = tuple(item for item in owned_sources if item.id in preset_ids)
    local_records = tuple(item for item in records if item.id not in preset_ids)
    decoded_catalog = BuiltInCatalog(source_catalog, owner.records)
    paused = pause_dormant_legacy_playlists(installed_catalog, legacy)
    decoded_overlay = ManagedState(
        schema_version=max(8, legacy.schema_version),
        sources=local_records,
        playlists=paused.playlists,
        templates=legacy.templates,
        quarantined_templates=legacy.quarantined_templates,
    )
    reconstructed = compile_engine_config(decoded_catalog, decoded_overlay)
    assert reconstructed == expected.original_runtime
    assert len(reconstructed.playlists) == 4
    assert [playlist.destination.external_id for playlist in reconstructed.playlists] == [
        playlist.destination.external_id for playlist in expected.original_runtime.playlists
    ]
    assert [p.duration_policy for p in reconstructed.playlists] == [
        p.duration_policy for p in expected.original_runtime.playlists
    ]
    assert [s.parser_id for s in reconstructed.sources] == [
        s.parser_id for s in expected.original_runtime.sources
    ]


def test_historical_long_metadata_unassigned_dormant_exception_and_dst_fold_preserved():
    original = _catalog_with_duration_exception()
    template = original.playlists[0]
    absent_from_current_default = next(
        source.id for source in original.sources if source.id not in template.default_source_ids
    )
    dormant = DurationPolicyException(
        "might_be_assigned_later",
        absent_from_current_default,
        time(12, 30, 5, 123456, fold=1),
        3600,
    )
    current = replace(
        template,
        display_name="Historical " + ("long" * 70),
        description="Full description " + ("a" * 500),
        duration_policy=DurationPolicy(1800, (dormant,), default_min_seconds=121),
        retention_hours=10_000,
        max_episodes=1_000,
    )
    codec = materialize_owner_templates((current,), sources=tuple(s.id for s in original.sources))
    (restored,) = OwnerTemplateSet.parse(codec.serialize()).records
    assert restored == current
    assert restored.id == current.id
    assert absent_from_current_default not in current.default_source_ids
    assert restored.duration_policy.exceptions == (dormant,)
    assert restored.duration_policy.exceptions[0].edition_local_time.fold == 1
    assert restored.duration_policy.exceptions[0].edition_local_time.microsecond == 123456
    assert restored.duration_policy.default_min_seconds == 121
    # Existing new-draft editor limits of 100 characters/100 episodes remain
    # untouched: this is separately versioned owner inventory evidence.
    assert len(restored.display_name) > 100
    assert restored.max_episodes > 100


def test_owner_policy_edits_are_explicit_and_cannot_rename_id_or_delete_bound_template():
    catalog = _catalog_with_duration_exception()
    sources = tuple(item.id for item in catalog.sources)
    initial = materialize_owner_templates(catalog.playlists, sources=sources)
    old = initial.records[0]
    edited = replace(old, display_name="Retitled, still owner-controlled")
    changed = initial.replace_one(old.id, edited, sources=sources)
    assert changed.records[0] == edited
    assert OwnerTemplateSet.parse(changed.serialize()) == changed
    assert initial.records[0] == old
    with pytest.raises(OwnerTemplateError, match="change"):
        initial.replace_one(old.id, replace(edited, id="different_id"), sources=sources)
    with pytest.raises(OwnerTemplateError, match="referenced"):
        changed.remove_unreferenced(old.id, managed_template_ids=(old.id,))
    with pytest.raises(OwnerTemplateError, match="referenced"):
        changed.remove_unreferenced(old.id, managed_template_ids=(), pending_template_ids=(old.id,))
    assert changed.remove_unreferenced(old.id, managed_template_ids=()).records == tuple(
        item for item in changed.records if item.id != old.id
    )
    # Removing the source used by an exception is invalid even if no current
    # destination selects it. Persisted owner references are not silently lost.
    missing = tuple(item for item in sources if item != SourceId("ser"))
    with pytest.raises(OwnerTemplateError, match="missing source"):
        changed.validate_sources(missing)


def test_duplicate_owner_template_ids_are_rejected():
    t = _catalog_with_duration_exception().playlists[0]
    with pytest.raises(OwnerTemplateError, match="Duplicate"):
        OwnerTemplateSet((t, t))


@pytest.mark.parametrize(
    "corruption",
    [
        lambda data: b"{}",
        lambda data: data + b"\n",
        lambda data: b'{"format":"a","version":1,"templates":[],"version":1}\n',
        lambda data: data.replace(b'"version":1', b'"version":true'),
        lambda data: data.replace(b'"version":1', b'"version":2'),
        lambda data: data.replace(b'"templates":[', b'"extra":0,"templates":['),
        lambda data: data.replace(b'"edition_fold":1', b'"edition_fold":2'),
        lambda data: data.replace(
            b'"edition_local_time":"12:00:00.000000"', b'"edition_local_time":"broken"'
        ),
        lambda data: data.replace(b'"ordering":"edition_at_desc"', b'"ordering":"unknown"'),
        lambda data: data.replace(b'"max_episodes":100', b'"max_episodes":true'),
        lambda data: b" " * (2 * 1024 * 1024 + 1),
        lambda data: b"\xff" + data[1:],
    ],
)
def test_corrupt_or_noncanonical_owner_template_rejected(corruption):
    catalog = _catalog_with_duration_exception()
    record = materialize_owner_templates(catalog.playlists, sources=tuple(
        s.id for s in catalog.sources
    )).serialize()
    with pytest.raises(OwnerTemplateError):
        OwnerTemplateSet.parse(corruption(record))


def test_future_template_id_is_not_inferred_from_legacy_image():
    catalog = _catalog_with_duration_exception()
    originals = materialize_owner_templates(catalog.playlists, sources=tuple(
        item.id for item in catalog.sources
    ))
    archived = originals.serialize()
    # The persisted record contains all values: no need to import BUILTIN_CATALOG
    # or inspect a newer container image to reopen it.
    restored = OwnerTemplateSet.parse(archived)
    assert json.loads(archived)["format"] == "news-bulletin-playlist.owner-templates"
    assert restored.records == originals.records
    assert OwnerTemplateSet.parse(OwnerTemplateSet(()).serialize()).records == ()
