"""Independently compiled read-only v11 state from a persisted candidate.

This is a new, opt-in *reader*, not a replacement for ManagedStateStore.
The only accepted persisted v11 input is the fully verified candidate envelope.
No startup call, state rewrite, Spotify operation or automatic recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.legacy_materialization import LegacyProvenance
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedPlaylist,
    ManagedSourceOverride,
    ManagedState,
    ManagedStateError,
    QuarantinedPlaylistTemplate,
    compile_engine_config,
    source_definition,
)
from news_bulletin_playlist.models import EngineConfig
from news_bulletin_playlist.v11_candidate import (
    V11Candidate,
    V11CandidateError,
    parse_v11_candidate,
)
from news_bulletin_playlist.v11_candidate_storage import read_v11_candidate

V11_STATE_SCHEMA_VERSION = 11


@dataclass(frozen=True, slots=True)
class V11ReadOnlyState:
    """Source of truth for future reader design, never live write authority."""

    schema_version: int
    installed_sources: tuple[InstalledSource, ...]
    source_provenance: tuple[LegacyProvenance, ...]
    playlist_examples: tuple[PlaylistTemplate, ...]
    example_provenance: tuple[LegacyProvenance, ...]
    local_drafts: tuple[LocalPlaylistTemplate, ...]
    managed_destinations: tuple[ManagedPlaylist, ...]
    source_overrides: tuple[ManagedSourceOverride, ...]
    quarantined_drafts: tuple[QuarantinedPlaylistTemplate, ...]
    runtime: EngineConfig
    legacy_managed_state_bytes: bytes

    @property
    def is_live_migration_authorized(self) -> bool:
        """A valid candidate alone NEVER authorizes a runtime cutover."""
        return False


def parse_v11_readonly_state(data: bytes) -> V11ReadOnlyState:
    """Parse a self-contained v11 candidate and independently compile its runtime."""
    return _state_from_candidate(parse_v11_candidate(data))


def load_v11_readonly_state(
    root: Path, *, recovery_inspection: bool = False
) -> V11ReadOnlyState:
    """Inspect a persisted v11 candidate, with no mutation of managed-state.json.

    Normal reads demand matching on-disk legacy state plus ordered-v2 evidence.
    Recovery inspection permits the old managed-state to differ, but requires
    the candidate and frozen-v2 evidence to be exactly consistent; this does
    not perform any restoration or authorize activation.
    """
    candidate = read_v11_candidate(root, require_legacy_unchanged=not recovery_inspection)
    return _state_from_candidate(candidate)


def _state_from_candidate(candidate: V11Candidate) -> V11ReadOnlyState:
    projection = candidate.projection
    if not projection.source_order_verified:
        raise V11CandidateError("unverified historical source ordering")
    if len(projection.installed_sources) != len(projection.source_provenance):
        raise V11CandidateError("source provenance is incomplete")
    if len(projection.playlist_examples) != len(projection.example_provenance):
        raise V11CandidateError("template provenance is incomplete")

    image_sources = tuple(
        replace(source_definition(source), enabled=True)
        for source, provenance in zip(
            projection.installed_sources, projection.source_provenance, strict=True
        )
        if provenance is LegacyProvenance.IMAGE_PRESET
    )
    local_sources = tuple(
        source
        for source, provenance in zip(
            projection.installed_sources, projection.source_provenance, strict=True
        )
        if provenance is LegacyProvenance.INSTALLATION
    )
    image_templates = tuple(
        template
        for template, provenance in zip(
            projection.playlist_examples, projection.example_provenance, strict=True
        )
        if provenance is LegacyProvenance.IMAGE_PRESET
    )
    legacy = projection.original_state
    derived_overlay = ManagedState(
        schema_version=legacy.schema_version,
        sources=local_sources,
        playlists=projection.managed_destinations,
        source_overrides=legacy.source_overrides,
        templates=projection.local_drafts,
        quarantined_templates=projection.quarantined_drafts,
    )
    try:
        independent_runtime = compile_engine_config(
            BuiltInCatalog(image_sources, image_templates), derived_overlay
        )
    except (ManagedStateError, ValueError) as exc:
        raise V11CandidateError("v11 ownership cannot compile independently") from exc
    if independent_runtime != projection.projected_runtime:
        raise V11CandidateError("v11 ownership changes historical runtime")

    return V11ReadOnlyState(
        schema_version=V11_STATE_SCHEMA_VERSION,
        installed_sources=projection.installed_sources,
        source_provenance=projection.source_provenance,
        playlist_examples=projection.playlist_examples,
        example_provenance=projection.example_provenance,
        local_drafts=projection.local_drafts,
        managed_destinations=projection.managed_destinations,
        source_overrides=legacy.source_overrides,
        quarantined_drafts=projection.quarantined_drafts,
        runtime=independent_runtime,
        legacy_managed_state_bytes=candidate.original_managed_state_bytes,
    )
