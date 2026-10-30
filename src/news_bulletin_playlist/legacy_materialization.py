"""Read-only preview of legacy catalog materialization for the future v11 migration.

An image-owned source/template must not disappear merely because a new image
removes its preset. This module freezes the exact *effective* legacy definitions
in memory, but deliberately does not introduce v11 persistence, rewrite state,
perform Spotify calls or change startup. Issue #552.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.custom_playlist_templates import (
    LocalPlaylistTemplateError,
    effective_playlist_templates,
)
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedState,
    ManagedStateError,
    compile_engine_config,
    effective_sources,
    source_definition,
)
from news_bulletin_playlist.models import EngineConfig


class LegacyProvenance(StrEnum):
    IMAGE_PRESET = "image_preset"
    INSTALLATION = "installation"


@dataclass(frozen=True, slots=True)
class FrozenLegacySource:
    provenance: LegacyProvenance
    installed: InstalledSource


@dataclass(frozen=True, slots=True)
class FrozenLegacyTemplate:
    provenance: LegacyProvenance
    definition: PlaylistTemplate


@dataclass(frozen=True, slots=True)
class LegacyMigrationPreview:
    """Immutable non-persistent plan with all existing installation records."""

    source_snapshots: tuple[FrozenLegacySource, ...]
    template_snapshots: tuple[FrozenLegacyTemplate, ...]
    original_state: ManagedState
    original_runtime: EngineConfig


def preview_legacy_materialization(
    catalog: BuiltInCatalog, state: ManagedState
) -> LegacyMigrationPreview:
    """Freeze old definitions, with no disk mutations or provider activity.

    Preserve source IDs, show IDs, disabled source settings, overrides,
    template duration exceptions, draft identities, managed destinations,
    quarantined entries and dormant assignments. Nothing is migrated until a
    future separately reviewed, versioned on-disk schema can represent these.
    """
    if state.schema_version not in {6, 7, 8, 9, 10}:
        raise ManagedStateError("legacy materialization requires managed-state v6–v10")

    runtime = compile_engine_config(catalog, state)
    effective = effective_sources(catalog, state)
    image_ids = {source.id for source in catalog.sources}
    frozen_sources: list[FrozenLegacySource] = []
    for source in effective:
        installed = InstalledSource.from_definition(source)
        if source_definition(installed) != source:
            raise ManagedStateError(f"legacy source {source.id} cannot be frozen without loss")
        frozen_sources.append(
            FrozenLegacySource(
                LegacyProvenance.IMAGE_PRESET
                if source.id in image_ids
                else LegacyProvenance.INSTALLATION,
                installed,
            )
        )

    try:
        templates = effective_playlist_templates(catalog, state.templates, sources=effective)
    except LocalPlaylistTemplateError as exc:
        raise ManagedStateError("legacy playlist template cannot be frozen safely") from exc
    image_template_ids = {item.id for item in catalog.playlists}
    frozen_templates = tuple(
        FrozenLegacyTemplate(
            LegacyProvenance.IMAGE_PRESET
            if item.id in image_template_ids
            else LegacyProvenance.INSTALLATION,
            item,
        )
        for item in templates
    )
    return LegacyMigrationPreview(
        source_snapshots=tuple(frozen_sources),
        template_snapshots=frozen_templates,
        original_state=state,
        original_runtime=runtime,
    )
