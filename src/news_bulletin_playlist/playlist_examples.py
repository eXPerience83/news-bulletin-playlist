"""Read-only playlist example resolution against installation-owned Spotify show identities.

Bundled image examples are never installation templates or Spotify destinations.
Source installation is always a separate, explicitly confirmed JSON import.
"""

from __future__ import annotations

from dataclasses import dataclass

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, PlaylistTemplate
from news_bulletin_playlist.models import PlaylistId, SourceDefinition, SourceId


@dataclass(frozen=True, slots=True)
class ExampleSourceResolution:
    source_pairs: tuple[tuple[SourceId, SourceId], ...]
    missing: tuple[SourceDefinition, ...]
    disabled: tuple[SourceDefinition, ...]

    @property
    def selected_ids(self) -> tuple[SourceId, ...]:
        return tuple(local for _, local in self.source_pairs)


def example_draft_id(example: PlaylistTemplate) -> PlaylistId:
    """One reserved, deterministic identity: repeat submissions cannot create copies."""
    return PlaylistId(f"custom_preset_{example.id}")


def _show_id(source: SourceDefinition) -> str:
    shows = {
        ref.external_id.strip()
        for ref in source.external_references
        if ref.system.casefold() == "spotify"
        and ref.resource_type.casefold() == "show"
        and ref.external_id.strip()
    }
    if len(shows) != 1:
        raise ValueError("source has an ambiguous Spotify show identity")
    return next(iter(shows))


def resolve_example_sources(
    example: PlaylistTemplate,
    installed: tuple[SourceDefinition, ...],
) -> ExampleSourceResolution:
    """Match the example's immutable source IDs to installed IDs by Spotify show ID.

    Never install, enable, drop, or rewrite any source. Missing/disabled requirements
    are returned for explicit UI guidance and must block draft creation.
    """
    by_show: dict[str, SourceDefinition] = {}
    for source in installed:
        show = _show_id(source)
        if show in by_show:
            raise ValueError("installed sources contain an ambiguous Spotify show identity")
        by_show[show] = source

    examples = {source.id: source for source in BUILTIN_CATALOG.sources}
    mapping: list[tuple[SourceId, SourceId]] = []
    missing: list[SourceDefinition] = []
    disabled: list[SourceDefinition] = []
    for source_id in example.default_source_ids:
        definition = examples.get(source_id)
        if definition is None:
            raise ValueError("playlist example references an unknown bundled source")
        found = by_show.get(_show_id(definition))
        if found is None:
            missing.append(definition)
        else:
            mapping.append((source_id, found.id))
            if not found.enabled:
                disabled.append(found)
    if len({local for _, local in mapping}) != len(mapping):
        raise ValueError("playlist example source mapping is ambiguous")
    return ExampleSourceResolution(tuple(mapping), tuple(missing), tuple(disabled))
