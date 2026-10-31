"""Standalone schema-v11 ownership document; never a live migration executor.

Unlike the v11 *candidate*, this document can be decoded without the old
managed-state bytes, the original data directory or the current image presets.
It remains read-only until a separately reviewed, reversible cutover exists.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.custom_playlist_templates import (
    LocalPlaylistTemplateError,
    effective_playlist_templates,
)
from news_bulletin_playlist.legacy_catalog_snapshot import (
    FrozenLegacySnapshotError,
    FrozenLegacyCatalogSnapshot,
    MAX_FROZEN_SNAPSHOT_BYTES,
    parse_legacy_snapshot,
)
from news_bulletin_playlist.legacy_materialization import LegacyProvenance
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedState,
    ManagedStateError,
    compile_engine_config,
    effective_sources,
    parse_managed_state,
    serialize_managed_state,
    source_definition,
)
from news_bulletin_playlist.models import EngineConfig, SourceDefinition
from news_bulletin_playlist.v11_candidate import (
    V11CandidateError,
    parse_v11_candidate,
)

OPERATIONAL_FORMAT = "news-bulletin-playlist.owned-operational-state"
OPERATIONAL_SCHEMA_VERSION = 11
MAX_OPERATIONAL_BYTES = 12 * 1024 * 1024


class V11OperationalError(ValueError):
    """Invalid ownership document must never trigger a fallback or state write."""


@dataclass(frozen=True, slots=True)
class V11OperationalState:
    schema_version: int
    historical_catalog: BuiltInCatalog
    managed_overlay: ManagedState
    runtime: EngineConfig
    installed_sources: tuple[InstalledSource, ...]
    source_provenance: tuple[LegacyProvenance, ...]
    playlist_examples: tuple[PlaylistTemplate, ...]
    example_provenance: tuple[LegacyProvenance, ...]
    migration_origin_sha256: str

    @property
    def is_live_migration_authorized(self) -> bool:
        return False


def build_v11_operational_from_candidate(candidate_bytes: bytes) -> bytes:
    """Normalize legacy data into a self-contained state, purely in memory."""
    try:
        candidate = parse_v11_candidate(candidate_bytes)
        snapshot = parse_legacy_snapshot(candidate.frozen_catalog_v2_bytes)
        if snapshot.source_order is None:
            raise V11OperationalError("ordered frozen-v2 source evidence is required")
        catalog = _historical_catalog(snapshot)
        overlay = serialize_managed_state(candidate.projection.original_state, catalog=catalog)
        encoded_catalog = json.loads(candidate.frozen_catalog_v2_bytes)
        document = {
            "format": OPERATIONAL_FORMAT,
            "schema_version": OPERATIONAL_SCHEMA_VERSION,
            "migration_origin_sha256": hashlib.sha256(
                candidate.original_managed_state_bytes
            ).hexdigest(),
            "installed_catalog": encoded_catalog,
            "managed_overlay": overlay,
        }
        payload = _canonical(document)
        result = parse_v11_operational(payload)
    except (FrozenLegacySnapshotError, ManagedStateError, V11CandidateError) as exc:
        raise V11OperationalError("cannot build a lossless operational state") from exc
    if result.runtime != candidate.projection.projected_runtime:
        raise V11OperationalError("v11 operational state changes original runtime")
    if result.managed_overlay != candidate.projection.original_state:
        raise V11OperationalError("v11 operational state changes original records")
    return payload


def parse_v11_operational(payload: bytes) -> V11OperationalState:
    """Strict, independent decode that never consults original-state files."""
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_OPERATIONAL_BYTES:
        raise V11OperationalError("invalid v11 operational size")
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise V11OperationalError("invalid v11 operational JSON") from exc
    if not isinstance(document, dict) or set(document) != {
        "format",
        "schema_version",
        "migration_origin_sha256",
        "installed_catalog",
        "managed_overlay",
    }:
        raise V11OperationalError("invalid v11 operational fields")
    if (
        document["format"] != OPERATIONAL_FORMAT
        or type(document["schema_version"]) is not int
        or document["schema_version"] != OPERATIONAL_SCHEMA_VERSION
    ):
        raise V11OperationalError("unsupported v11 operational format")
    try:
        catalog_data = document["installed_catalog"]
        if not isinstance(catalog_data, dict):
            raise V11OperationalError("invalid frozen operational catalog")
        catalog_bytes = _canonical(catalog_data)
        if len(catalog_bytes) > MAX_FROZEN_SNAPSHOT_BYTES:
            raise V11OperationalError("frozen operational catalog too large")
        snapshot = parse_legacy_snapshot(catalog_bytes)
        if snapshot.source_order is None:
            raise V11OperationalError("v11 requires verified historical source order")
        digest = document["migration_origin_sha256"]
        if digest != snapshot.managed_state_sha256:
            raise V11OperationalError("v11 historical origin digest mismatch")
        catalog = _historical_catalog(snapshot)
        overlay = parse_managed_state(document["managed_overlay"], catalog=catalog)
        if overlay.schema_version != snapshot.managed_schema_version:
            raise V11OperationalError("v11 overlay historical schema mismatch")
        if serialize_managed_state(overlay, catalog=catalog) != document["managed_overlay"]:
            raise V11OperationalError("noncanonical v11 managed overlay")
        runtime = compile_engine_config(catalog, overlay)
        ordered = tuple(snapshot.source_order)
        if tuple(item.id for item in runtime.sources) != ordered:
            raise V11OperationalError("v11 source processing order changed")
        frozen_sources = {item.installed.id: item.installed for item in snapshot.sources}
        actual_sources = {
            source.id: InstalledSource.from_definition(source)
            for source in effective_sources(catalog, overlay)
        }
        if frozen_sources != actual_sources:
            raise V11OperationalError("v11 effective source definition drift")
        templates = effective_playlist_templates(
            catalog, overlay.templates, sources=runtime.sources
        )
        if templates != tuple(item.definition for item in snapshot.templates):
            raise V11OperationalError("v11 playlist example definition drift")
        if payload != _canonical(document):
            raise V11OperationalError("noncanonical v11 operational JSON")
    except (FrozenLegacySnapshotError, ManagedStateError, LocalPlaylistTemplateError) as exc:
        raise V11OperationalError("v11 operational state cannot be validated") from exc

    by_id = {item.installed.id: item for item in snapshot.sources}
    ordered_records = tuple(by_id[source_id] for source_id in snapshot.source_order)
    return V11OperationalState(
        schema_version=OPERATIONAL_SCHEMA_VERSION,
        historical_catalog=catalog,
        managed_overlay=overlay,
        runtime=runtime,
        installed_sources=tuple(item.installed for item in ordered_records),
        source_provenance=tuple(item.provenance for item in ordered_records),
        playlist_examples=templates,
        example_provenance=tuple(item.provenance for item in snapshot.templates),
        migration_origin_sha256=digest,
    )


def _historical_catalog(snapshot: FrozenLegacyCatalogSnapshot) -> BuiltInCatalog:
    if snapshot.source_order is None:
        raise V11OperationalError("ordered historical catalog is required")
    by_id = {item.installed.id: item for item in snapshot.sources}
    historical_sources: tuple[SourceDefinition, ...] = tuple(
        replace(source_definition(by_id[source_id].installed), enabled=True)
        for source_id in snapshot.source_order
        if by_id[source_id].provenance is LegacyProvenance.IMAGE_PRESET
    )
    image_templates = tuple(
        item.definition
        for item in snapshot.templates
        if item.provenance is LegacyProvenance.IMAGE_PRESET
    )
    return BuiltInCatalog(historical_sources, image_templates)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise V11OperationalError("duplicate v11 operational JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise V11OperationalError("invalid v11 operational numeric constant")


def _canonical(value: object) -> bytes:
    encoded = (
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_OPERATIONAL_BYTES:
        raise V11OperationalError("v11 operational document exceeds size limit")
    return encoded
