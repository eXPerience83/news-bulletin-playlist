"""Read-only v11 ownership projection from a frozen historical catalog.

This is neither a v11 managed-state serializer nor a migration/startup hook.
It must never access live Spotify, TrueNAS or persistent installation files.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.custom_playlist_templates import (
    LocalPlaylistTemplate,
    LocalPlaylistTemplateError,
    effective_playlist_templates,
)
from news_bulletin_playlist.legacy_catalog_snapshot import (
    FrozenLegacySnapshotError,
    parse_legacy_snapshot,
)
from news_bulletin_playlist.legacy_materialization import LegacyProvenance
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedPlaylist,
    ManagedState,
    ManagedStateError,
    QuarantinedPlaylistTemplate,
    compile_engine_config,
    effective_sources,
    parse_managed_state,
    source_definition,
)
from news_bulletin_playlist.models import EngineConfig, SourceDefinition


@dataclass(frozen=True, slots=True)
class V11LegacyProjection:
    """Candidate ownership model in memory; no schema-11 disk writes."""

    original_state: ManagedState
    installed_sources: tuple[InstalledSource, ...]
    source_provenance: tuple[LegacyProvenance, ...]
    playlist_examples: tuple[PlaylistTemplate, ...]
    example_provenance: tuple[LegacyProvenance, ...]
    local_drafts: tuple[LocalPlaylistTemplate, ...]
    managed_destinations: tuple[ManagedPlaylist, ...]
    quarantined_drafts: tuple[QuarantinedPlaylistTemplate, ...]
    projected_runtime: EngineConfig
    source_order_verified: bool
    original_state_sha256: str


def project_v11_legacy(
    frozen_catalog_bytes: bytes, original_managed_state_bytes: bytes
) -> V11LegacyProjection:
    """Reconstruct v2 in historical order; v1 remains explicitly unverified.

    Source order in v1 is canonical Spotify-show order, not historical image
    iteration order. Only a complete, separately validated v2 order can
    support exact projected runtime equivalence.
    """
    snapshot = parse_legacy_snapshot(frozen_catalog_bytes)
    if not isinstance(original_managed_state_bytes, bytes):
        raise FrozenLegacySnapshotError("invalid legacy state bytes")
    digest = hashlib.sha256(original_managed_state_bytes).hexdigest()
    if digest != snapshot.managed_state_sha256:
        raise FrozenLegacySnapshotError("frozen catalog does not match managed state")

    ordered_records = snapshot.sources
    if snapshot.source_order is not None:
        by_id = {item.installed.id: item for item in snapshot.sources}
        ordered_records = tuple(by_id[source_id] for source_id in snapshot.source_order)
    image_sources: tuple[SourceDefinition, ...] = tuple(
        replace(source_definition(item.installed), enabled=True)
        for item in ordered_records
        if item.provenance is LegacyProvenance.IMAGE_PRESET
    )
    image_templates = tuple(
        item.definition
        for item in snapshot.templates
        if item.provenance is LegacyProvenance.IMAGE_PRESET
    )
    try:
        historical_catalog = BuiltInCatalog(image_sources, image_templates)
        document = json.loads(
            original_managed_state_bytes.decode("utf-8"), object_pairs_hook=_unique_object
        )
        state = parse_managed_state(document, catalog=historical_catalog)
        if state.schema_version != snapshot.managed_schema_version:
            raise FrozenLegacySnapshotError("managed schema changed since snapshot")
        sources = effective_sources(historical_catalog, state)
        templates = effective_playlist_templates(
            historical_catalog, state.templates, sources=sources
        )
        runtime = compile_engine_config(historical_catalog, state)
        reconstructed = {item.id: InstalledSource.from_definition(item) for item in sources}
    except (ValueError, TypeError, UnicodeError, RecursionError, ManagedStateError) as exc:
        raise FrozenLegacySnapshotError("legacy snapshot cannot be reconstructed safely") from exc
    except LocalPlaylistTemplateError as exc:
        raise FrozenLegacySnapshotError("legacy templates cannot be reconstructed safely") from exc

    frozen = {item.installed.id: item.installed for item in snapshot.sources}
    if len(frozen) != len(snapshot.sources) or frozen != reconstructed:
        raise FrozenLegacySnapshotError("frozen sources do not reproduce legacy configuration")
    if templates != tuple(item.definition for item in snapshot.templates):
        raise FrozenLegacySnapshotError("frozen templates do not reproduce legacy configuration")
    show_ids = [item.spotify_show_id for item in frozen.values()]
    if len(set(show_ids)) != len(show_ids):
        raise FrozenLegacySnapshotError("frozen sources contain duplicate Spotify shows")
    source_order_verified = snapshot.source_order is not None
    if source_order_verified and tuple(source.id for source in runtime.sources) != (
        snapshot.source_order
    ):
        raise FrozenLegacySnapshotError("frozen source order differs from reconstructed runtime")

    return V11LegacyProjection(
        original_state=state,
        installed_sources=tuple(item.installed for item in ordered_records),
        source_provenance=tuple(item.provenance for item in ordered_records),
        playlist_examples=templates,
        example_provenance=tuple(item.provenance for item in snapshot.templates),
        local_drafts=state.templates,
        managed_destinations=state.playlists,
        quarantined_drafts=state.quarantined_templates,
        projected_runtime=runtime,
        source_order_verified=source_order_verified,
        original_state_sha256=digest,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FrozenLegacySnapshotError("duplicate field in legacy managed state")
        result[key] = value
    return result
