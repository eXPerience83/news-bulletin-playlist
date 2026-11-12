"""Standalone schema-v11 owned configuration document.

The historical catalog and origin digest are frozen on first migration.
The installation-owned overlay may be edited without consulting the old
managed-state bytes, the original directory or changing image presets.
Live startup migration is a separate integration decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace

from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.custom_cover_assets import is_uploaded_cover_id
from news_bulletin_playlist.custom_playlist_templates import (
    LocalPlaylistTemplateError,
    effective_playlist_templates,
)
from news_bulletin_playlist.legacy_catalog_snapshot import (
    MAX_FROZEN_SNAPSHOT_BYTES,
    FrozenLegacyCatalogSnapshot,
    FrozenLegacySnapshotError,
    parse_legacy_snapshot,
)
from news_bulletin_playlist.legacy_materialization import (
    LegacyProvenance,
    pause_dormant_legacy_playlists,
)
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedState,
    ManagedStateError,
    compile_engine_config,
    parse_managed_state,
    serialize_managed_state,
    source_definition,
)
from news_bulletin_playlist.models import EngineConfig, SourceDefinition
from news_bulletin_playlist.owner_inventory import OwnerInventory, OwnerInventoryError
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
    active_catalog: BuiltInCatalog
    managed_overlay: ManagedState
    runtime: EngineConfig
    installed_sources: tuple[InstalledSource, ...]
    source_provenance: tuple[LegacyProvenance, ...]
    playlist_examples: tuple[PlaylistTemplate, ...]
    example_provenance: tuple[LegacyProvenance, ...]
    migration_origin_sha256: str
    owner_inventory: OwnerInventory
    inherited_cover_ids: tuple[str, ...] = ()

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
        normalized = pause_dormant_legacy_playlists(catalog, candidate.projection.original_state)
        overlay = serialize_managed_state(normalized, catalog=catalog)
        encoded_catalog = json.loads(candidate.frozen_catalog_v2_bytes)
        inherited_cover_ids = sorted(
            cover_id
            for cover_id in (
                {item.definition.cover_id for item in snapshot.templates}
                | {item.cover_id for item in candidate.projection.original_state.playlists}
                | {item.cover_id for item in candidate.projection.original_state.templates}
            )
            if not is_uploaded_cover_id(cover_id)
        )
        document = {
            "format": OPERATIONAL_FORMAT,
            "schema_version": OPERATIONAL_SCHEMA_VERSION,
            "migration_origin_sha256": hashlib.sha256(
                candidate.original_managed_state_bytes
            ).hexdigest(),
            "installed_catalog": encoded_catalog,
            "managed_overlay": overlay,
            # Strict independent owner records for a future atomic authority
            # transition. The old frozen catalog is audit provenance only.
            "owner_inventory": OwnerInventory.from_snapshot(snapshot).to_document(),
            # Immutable v11 evidence that some bundled IDs must never fall back
            # to a later image if /data/migrated-covers/index.json goes missing.
            **({"inherited_cover_ids": inherited_cover_ids} if inherited_cover_ids else {}),
        }
        payload = _canonical(document)
        result = parse_v11_operational(payload)
    except (
        FrozenLegacySnapshotError,
        ManagedStateError,
        V11CandidateError,
        OwnerInventoryError,
    ) as exc:
        raise V11OperationalError("cannot build a lossless operational state") from exc
    if result.runtime != candidate.projection.projected_runtime:
        raise V11OperationalError("v11 operational state changes original runtime")
    if result.managed_overlay != normalized:
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
    required_fields = {
        "format",
        "schema_version",
        "migration_origin_sha256",
        "installed_catalog",
        "managed_overlay",
        "owner_inventory",
    }
    # An owner inventory is mandatory: accepting missing data as an old beta
    # would silently revert its source/template ownership to frozen presets.
    # Older v11 beta authority requires an explicit reviewed conversion.
    if not isinstance(document, dict):
        raise V11OperationalError("missing or invalid v11 owner inventory fields")
    # An unedited conversion requires exact match with the immutable legacy
    # witness. Only a versioned owner mutation can diverge subsequently.
    owned_cover_ids = document.get("inherited_cover_ids")
    if owned_cover_ids is not None and (
        not isinstance(owned_cover_ids, list)
        or len(owned_cover_ids) > 100
        or any(not isinstance(cover_id, str) for cover_id in owned_cover_ids)
        or len(set(owned_cover_ids)) != len(owned_cover_ids)
        or owned_cover_ids != sorted(owned_cover_ids)
        or any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", cover_id) is None
            or is_uploaded_cover_id(cover_id)
            for cover_id in owned_cover_ids
        )
    ):
        raise V11OperationalError("invalid inherited cover inventory")
    recognized_fields = required_fields | (
        {"inherited_cover_ids"} if owned_cover_ids is not None else set()
    )
    owner_revision = document.get("owner_revision")
    if owner_revision is None:
        if set(document) != recognized_fields:
            raise V11OperationalError("missing or invalid v11 owner inventory fields")
    elif (
        set(document) != recognized_fields | {"owner_revision"}
        or type(owner_revision) is not int
        or not 1 <= owner_revision <= 1_000_000
    ):
        raise V11OperationalError("invalid versioned owner inventory revision")
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
        if owned_cover_ids is not None and not {
            item.definition.cover_id
            for item in snapshot.templates
            if not is_uploaded_cover_id(item.definition.cover_id)
        }.issubset(owned_cover_ids):
            raise V11OperationalError("inherited cover inventory omits historical template")
        owner_inventory = OwnerInventory.parse(document["owner_inventory"])
        if owner_revision is None:
            owner_inventory.validate_against(snapshot)
        digest = document["migration_origin_sha256"]
        if digest != snapshot.managed_state_sha256:
            raise V11OperationalError("v11 historical origin digest mismatch")
        catalog = _historical_catalog(snapshot)
        # The owner inventory, not the legacy image-preset reconstruction, is
        # now the runtime source of truth. Historical catalog is retained only
        # for the provenance record and initial migration equivalence.
        active_catalog = _owner_active_catalog(snapshot, owner_inventory)
        overlay = parse_managed_state(document["managed_overlay"], catalog=active_catalog)
        if overlay.schema_version < snapshot.managed_schema_version:
            raise V11OperationalError("v11 overlay cannot downgrade its original schema")
        if serialize_managed_state(overlay, catalog=active_catalog) != document["managed_overlay"]:
            raise V11OperationalError("noncanonical v11 managed overlay")
        runtime = compile_engine_config(active_catalog, overlay)
        # The original historical order is frozen, not a constraint on later
        # installation-owned source/template edits. Only image-preset sources
        # remain fixed in their original relative order; newly installed
        # sources may be added or removed through the validated overlay.
        image_ids = tuple(source.id for source in active_catalog.sources)
        if tuple(item.id for item in runtime.sources[: len(image_ids)]) != image_ids:
            raise V11OperationalError("v11 installed catalog source order changed")
        templates = effective_playlist_templates(
            active_catalog, overlay.templates, sources=runtime.sources
        )
        if payload != _canonical(document):
            raise V11OperationalError("noncanonical v11 operational JSON")
    except (
        FrozenLegacySnapshotError,
        ManagedStateError,
        LocalPlaylistTemplateError,
        OwnerInventoryError,
    ) as exc:
        raise V11OperationalError("v11 operational state cannot be validated") from exc

    by_id = {item.installed.id: item for item in snapshot.sources}
    ordered_records = tuple(by_id[source_id] for source_id in snapshot.source_order)
    return V11OperationalState(
        schema_version=OPERATIONAL_SCHEMA_VERSION,
        historical_catalog=catalog,
        active_catalog=active_catalog,
        managed_overlay=overlay,
        runtime=runtime,
        installed_sources=tuple(item.installed for item in ordered_records),
        source_provenance=tuple(item.provenance for item in ordered_records),
        playlist_examples=templates,
        example_provenance=tuple(item.provenance for item in snapshot.templates),
        migration_origin_sha256=digest,
        owner_inventory=owner_inventory,
        inherited_cover_ids=tuple(owned_cover_ids or ()),
    )


def update_v11_operational(payload: bytes, overlay: ManagedState) -> bytes:
    """Serialize a validated owner edit without replacing the historical catalog.

    This is a pure transformation; the caller must perform the durable
    compare-and-swap publication and prevent legacy writers separately.
    """
    current = parse_v11_operational(payload)
    if not isinstance(overlay, ManagedState):
        raise V11OperationalError("invalid v11 managed overlay")
    if overlay.schema_version < current.managed_overlay.schema_version:
        raise V11OperationalError("v11 managed overlay schema cannot downgrade")
    try:
        document = json.loads(payload)
        document["managed_overlay"] = serialize_managed_state(
            overlay, catalog=current.active_catalog
        )
        next_payload = _canonical(document)
        verified = parse_v11_operational(next_payload)
    except (ManagedStateError, ValueError, TypeError, RecursionError) as exc:
        raise V11OperationalError("v11 edit cannot be represented losslessly") from exc
    if verified.managed_overlay != overlay:
        raise V11OperationalError("v11 edit changed managed state")
    if verified.historical_catalog != current.historical_catalog:
        raise V11OperationalError("v11 edit changed historical catalog")
    if verified.active_catalog != current.active_catalog:
        raise V11OperationalError("v11 edit changed owner-backed active catalog")
    if verified.migration_origin_sha256 != current.migration_origin_sha256:
        raise V11OperationalError("v11 edit changed migration origin")
    if verified.owner_inventory != current.owner_inventory:
        raise V11OperationalError("v11 overlay edit changed installed owner inventory")
    return next_payload


def update_v11_owner_inventory(
    payload: bytes, edited: OwnerInventory, *, managed_overlay: ManagedState | None = None
) -> bytes:
    """Publish a validated owner-inventory revision without rewriting legacy evidence.

    A revision is explicit and strictly increasing. The initial conversion is
    still required to exactly match its immutable historical witness.
    """
    current = parse_v11_operational(payload)
    if not isinstance(edited, OwnerInventory):
        raise V11OperationalError("invalid owner inventory edit")
    updated_overlay = current.managed_overlay if managed_overlay is None else managed_overlay
    if edited == current.owner_inventory and updated_overlay == current.managed_overlay:
        return payload
    try:
        document = json.loads(payload)
        document["owner_inventory"] = edited.to_document()
        # Update owner mode and retire its legacy override in one durable CAS.
        # Owner catalog and managed projection must validate as *one* candidate:
        # a newly imported template may reference source records added to the
        # owner inventory by this same atomic CAS.
        snapshot = parse_legacy_snapshot(_canonical(document["installed_catalog"]))
        next_catalog = _owner_active_catalog(snapshot, edited)
        document["managed_overlay"] = serialize_managed_state(updated_overlay, catalog=next_catalog)
        document["owner_revision"] = document.get("owner_revision", 0) + 1
        candidate = _canonical(document)
        verified = parse_v11_operational(candidate)
    except (ValueError, TypeError, RecursionError) as exc:
        raise V11OperationalError("owner inventory edit is not valid") from exc
    if (
        verified.managed_overlay != updated_overlay
        or verified.migration_origin_sha256 != current.migration_origin_sha256
        or verified.historical_catalog != current.historical_catalog
        or verified.owner_inventory != edited
    ):
        raise V11OperationalError("owner edit changed historical evidence or requested policy")
    return candidate


def _owner_active_catalog(
    snapshot: FrozenLegacyCatalogSnapshot,
    owned: OwnerInventory,
) -> BuiltInCatalog:
    """Compile from owner-persisted definitions, never image presets.

    Installation-provenance sources stay in the existing managed overlay;
    image-origin sources are sourced from owner records at their exact original
    positions. Future owner edits must be a separate CAS-authorized operation.
    """
    original_image_ids = {
        item.installed.id
        for item in snapshot.sources
        if item.provenance is LegacyProvenance.IMAGE_PRESET
    }
    historical_ids = {item.installed.id for item in snapshot.sources}
    ordered_sources = owned.source_order.definitions(owned.sources)
    # Historical custom sources remain represented in managed_overlay.sources,
    # whereas newly owner-installed sources belong to this active catalog.
    # Do not duplicate historical custom records or silently hide new sources.
    return BuiltInCatalog(
        tuple(
            source
            for source in ordered_sources
            if source.id in original_image_ids or source.id not in historical_ids
        ),
        owned.templates.records,
        allow_disabled_sources=True,
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
