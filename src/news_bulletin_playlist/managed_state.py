"""Persistent per-installation choices layered over the built-in catalog."""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.custom_playlist_templates import (
    LocalPlaylistTemplate,
    LocalPlaylistTemplateError,
    effective_playlist_templates,
)
from news_bulletin_playlist.custom_source_policy import custom_strategy_allowed
from news_bulletin_playlist.file_durability import fsync_directory
from news_bulletin_playlist.managed_duration import (
    validate_duration_range,
    validate_persisted_duration_seconds,
    validate_persisted_min_duration_seconds,
)
from news_bulletin_playlist.models import (
    AdapterId,
    CountryCode,
    DestinationReference,
    DurationPolicy,
    EditorialScope,
    EngineConfig,
    ExternalReference,
    LanguageTag,
    OrderingPolicy,
    ParserId,
    PlaylistDefinition,
    PlaylistId,
    PublicReleaseMode,
    SourceDefinition,
    SourceId,
    SourceOrigin,
    SourceOriginKind,
    SourceSelection,
    TemporalStrategy,
)
from news_bulletin_playlist.spotify.identifiers import is_spotify_id

MANAGED_STATE_FILENAME = "managed-state.json"
_MANAGED_STATE_SCHEMA_VERSION = 6
_CUSTOM_TEMPLATES_SCHEMA_VERSION = 7
_LEGACY_MANAGED_STATE_SCHEMA_VERSION = 1
_LEGACY_V2_MANAGED_STATE_SCHEMA_VERSION = 2
_LEGACY_V1_DEFAULT_MAX_DURATION_SECONDS = 1800
_MAX_SOURCE_DISPLAY_NAME_LENGTH = 120
_MAX_SOURCE_TIMEZONE_LENGTH = 128
_SOURCE_LANGUAGE_TAG = re.compile(r"[a-z]{2,3}(?:-(?:[A-Z]{2}|[0-9]{3}))?")


class ManagedStateError(ValueError):
    """Actionable error for invalid or unsafe managed installation state."""


def _temporal_strategy(value: object) -> TemporalStrategy:
    if not isinstance(value, str):
        raise ManagedStateError("unknown temporal_strategy")
    try:
        return TemporalStrategy(value)
    except ValueError, TypeError:
        raise ManagedStateError("unknown temporal_strategy") from None


@dataclass(frozen=True, slots=True)
class ManagedPlaylist:
    id: PlaylistId
    template_id: PlaylistId
    enabled: bool
    display_name: str
    description: str
    cover_id: str
    source_ids: tuple[SourceId, ...]
    destination: DestinationReference
    retention_hours: int
    max_episodes: int
    max_duration_seconds: int = _LEGACY_V1_DEFAULT_MAX_DURATION_SECONDS
    min_duration_seconds: int = 0


@dataclass(frozen=True, slots=True)
class ManagedSource:
    id: SourceId
    spotify_show_id: str
    display_name: str
    origin: SourceOrigin
    language: LanguageTag
    editorial_scope: EditorialScope
    timezone: str
    parser_id: ParserId
    collection_filter_id: str | None
    enabled: bool
    temporal_strategy: TemporalStrategy = TemporalStrategy.AUTO
    public_release_mode: PublicReleaseMode = PublicReleaseMode.INSTANT


@dataclass(frozen=True, slots=True)
class ManagedSourceOverride:
    source_id: SourceId
    public_release_mode: PublicReleaseMode


def _public_release_mode(value: object) -> PublicReleaseMode:
    if not isinstance(value, str):
        raise ManagedStateError("unknown public_release_mode")
    try:
        return PublicReleaseMode(value)
    except ValueError:
        raise ManagedStateError("unknown public_release_mode") from None


def _validate_overrides(
    overrides: tuple[ManagedSourceOverride, ...], catalog: BuiltInCatalog
) -> None:
    known = {source.id for source in catalog.sources}
    seen: set[SourceId] = set()
    for override in overrides:
        if override.source_id not in known:
            raise ManagedStateError("unknown built-in source override")
        if override.source_id in seen:
            raise ManagedStateError("duplicate built-in source override")
        if not isinstance(override.public_release_mode, PublicReleaseMode):
            raise ManagedStateError("unknown public_release_mode")
        seen.add(override.source_id)


@dataclass(frozen=True, slots=True)
class QuarantinedPlaylistTemplate:
    """Preserve an invalid local draft losslessly as JSON data, never as active config."""

    source_index: int
    reason: str
    raw_json: str

    @property
    def template_id(self) -> str | None:
        value = json.loads(self.raw_json)
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            return value["id"]
        return None


def _quarantined(raw: object, index: int, reason: str) -> QuarantinedPlaylistTemplate:
    return QuarantinedPlaylistTemplate(
        source_index=index,
        reason=reason,
        raw_json=json.dumps(raw, sort_keys=True, ensure_ascii=False),
    )


@dataclass(frozen=True, slots=True)
class ManagedState:
    schema_version: int = _MANAGED_STATE_SCHEMA_VERSION
    sources: tuple[ManagedSource, ...] = ()
    playlists: tuple[ManagedPlaylist, ...] = ()
    source_overrides: tuple[ManagedSourceOverride, ...] = ()
    templates: tuple[LocalPlaylistTemplate, ...] = ()
    quarantined_templates: tuple[QuarantinedPlaylistTemplate, ...] = ()


class ManagedStateStore:
    """Load and atomically replace the small installation-owned JSON overlay."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ManagedState:
        if self.path.is_symlink():
            raise ManagedStateError(f"managed state path is not a regular file: {self.path}")
        if not self.path.exists():
            return ManagedState()
        if not self.path.is_file():
            raise ManagedStateError(f"managed state path is not a regular file: {self.path}")
        try:
            payload = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object,
            )
        except (OSError, json.JSONDecodeError, ManagedStateError) as exc:
            raise ManagedStateError(f"could not load managed state: {exc}") from exc

        state, migrated_from = _parse_managed_state_with_version(payload)
        if migrated_from is not None:
            try:
                self.save(state)
            except (OSError, ManagedStateError) as exc:
                raise ManagedStateError(
                    "managed-state migration could not be persisted atomically; "
                    "original state preserved"
                ) from exc
        return state

    def save(self, state: ManagedState) -> None:
        payload = serialize_managed_state(state)
        document = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ManagedStateError(f"managed state path is not a regular file: {self.path}")
        temp_path = parent / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            os.chmod(self.path, 0o600)
            fsync_directory(parent)
        finally:
            with suppress(FileNotFoundError):
                temp_path.unlink()


def activate_template(
    template: PlaylistTemplate,
    destination_external_id: str,
    *,
    playlist_id: PlaylistId | None = None,
) -> ManagedPlaylist:
    """Snapshot template defaults into installation-owned state on first activation."""
    external_id = destination_external_id.strip()
    if not external_id:
        raise ManagedStateError("destination external_id must not be empty")
    return ManagedPlaylist(
        id=template.id if playlist_id is None else playlist_id,
        template_id=template.id,
        enabled=True,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        destination=DestinationReference(AdapterId("spotify"), external_id),
        retention_hours=template.retention_hours,
        max_episodes=template.max_episodes,
        max_duration_seconds=template.duration_policy.default_max_seconds,
        min_duration_seconds=template.duration_policy.default_min_seconds,
    )


def compile_engine_config(catalog: BuiltInCatalog, state: ManagedState) -> EngineConfig:
    """Compile immutable catalog knowledge plus local choices into the existing engine model."""
    sources = effective_sources(catalog, state)
    source_ids = {source.id for source in sources}
    enabled_source_ids = {source.id for source in sources if source.enabled}
    try:
        templates = {
            template.id: template
            for template in effective_playlist_templates(catalog, state.templates, sources=sources)
        }
    except LocalPlaylistTemplateError as exc:
        raise ManagedStateError(str(exc)) from exc
    playlists: list[PlaylistDefinition] = []
    seen_ids: set[PlaylistId] = set()
    seen_destinations: set[tuple[AdapterId, str]] = set()

    quarantined_ids = {
        item.template_id
        for item in state.quarantined_templates
        if item.template_id is not None and item.template_id not in templates
    }
    for managed in state.playlists:
        # A corrupted custom draft must never reconcile by a made-up/default policy.
        # Excluding its destination causes zero provider writes, without removing the
        # managed record or affecting unrelated destinations.
        if str(managed.template_id) in quarantined_ids:
            continue
        if managed.id in seen_ids:
            raise ManagedStateError(f"duplicate managed playlist id: {managed.id}")
        seen_ids.add(managed.id)
        template = templates.get(managed.template_id)
        if template is None:
            raise ManagedStateError(
                f"managed playlist {managed.id} references unknown template {managed.template_id}"
            )
        unknown_sources = [
            source_id for source_id in managed.source_ids if source_id not in source_ids
        ]
        if unknown_sources:
            raise ManagedStateError(
                f"managed playlist {managed.id} references unknown source {unknown_sources[0]}"
            )
        selected_sources = tuple(
            source_id for source_id in managed.source_ids if source_id in enabled_source_ids
        )
        if managed.enabled and not selected_sources:
            raise ManagedStateError(
                f"enabled managed playlist {managed.id} must select at least one enabled source"
            )
        destination_key = (
            managed.destination.adapter_id,
            managed.destination.external_id,
        )
        if managed.enabled and destination_key in seen_destinations:
            raise ManagedStateError(
                f"duplicate enabled destination for managed playlist {managed.id}"
            )
        if managed.enabled:
            seen_destinations.add(destination_key)

        max_duration_seconds = managed.max_duration_seconds
        try:
            validate_duration_range(managed.min_duration_seconds, max_duration_seconds)
        except ValueError as exc:
            raise ManagedStateError(f"managed playlist {managed.id}: {exc}") from exc
        duration_policy = DurationPolicy(
            default_max_seconds=max_duration_seconds,
            default_min_seconds=managed.min_duration_seconds,
            exceptions=tuple(
                exception
                for exception in template.duration_policy.exceptions
                if exception.max_seconds > max_duration_seconds
            ),
        )

        playlists.append(
            PlaylistDefinition(
                id=managed.id,
                display_name=managed.display_name,
                description=managed.description,
                countries=template.countries,
                languages=template.languages,
                enabled=managed.enabled,
                source_selection=SourceSelection(explicit=selected_sources),
                destination=managed.destination,
                retention_hours=managed.retention_hours,
                max_episodes=managed.max_episodes,
                ordering=template.ordering,
                duration_policy=duration_policy,
            )
        )

    return EngineConfig(
        sources=sources,
        playlists=tuple(playlists),
    )


def effective_sources(catalog: BuiltInCatalog, state: ManagedState) -> tuple[SourceDefinition, ...]:
    """Compile a deterministic, immutable built-in plus installation-owned source set."""
    _validate_overrides(state.source_overrides, catalog)
    overrides = {item.source_id: item.public_release_mode for item in state.source_overrides}
    built_ins = tuple(
        replace(source, public_release_mode=overrides[source.id])
        if source.id in overrides
        else source
        for source in catalog.sources
    )
    built_in_ids = {source.id for source in catalog.sources}
    built_in_shows = {
        reference.external_id.strip()
        for source in catalog.sources
        for reference in source.external_references
        if reference.system.casefold() == "spotify" and reference.resource_type.casefold() == "show"
    }
    seen_ids: set[SourceId] = set()
    seen_shows: set[str] = set()
    custom: list[SourceDefinition] = []
    for index, managed in enumerate(state.sources):
        path = f"sources[{index}]"
        _validate_source(managed, path)
        if managed.id in seen_ids:
            raise ManagedStateError(f"{path}.id: duplicate custom source id {managed.id}")
        if managed.spotify_show_id in seen_shows:
            raise ManagedStateError(
                f"{path}.spotify_show_id: duplicate custom show id {managed.spotify_show_id}"
            )
        if managed.id in built_in_ids:
            raise ManagedStateError(f"{path}.id: conflicts with built-in source {managed.id}")
        if managed.spotify_show_id in built_in_shows:
            raise ManagedStateError(f"{path}.spotify_show_id: conflicts with built-in Spotify show")
        seen_ids.add(managed.id)
        seen_shows.add(managed.spotify_show_id)
        custom.append(
            SourceDefinition(
                id=managed.id,
                display_name=managed.display_name,
                origin=managed.origin,
                countries=(),
                languages=(managed.language,),
                timezone=ZoneInfo(managed.timezone),
                enabled=managed.enabled,
                parser_id=managed.parser_id,
                temporal_strategy=managed.temporal_strategy,
                public_release_mode=managed.public_release_mode,
                collection_filter_id=managed.collection_filter_id,
                editorial_scope=managed.editorial_scope,
                external_references=(
                    ExternalReference("spotify", "show", managed.spotify_show_id),
                ),
            )
        )
    return built_ins + tuple(sorted(custom, key=lambda source: str(source.id)))


def parse_managed_state(payload: object) -> ManagedState:
    """Parse current or legacy managed state into the current in-memory schema."""
    state, _ = _parse_managed_state_with_version(payload)
    return state


def _parse_managed_state_with_version(payload: object) -> tuple[ManagedState, int | None]:
    root = _mapping(payload, "managed state")
    source_schema_version = _integer(
        _required(root, "schema_version", "managed state"), "schema_version"
    )
    if source_schema_version not in {
        _LEGACY_MANAGED_STATE_SCHEMA_VERSION,
        _LEGACY_V2_MANAGED_STATE_SCHEMA_VERSION,
        3,
        4,
        5,
        _MANAGED_STATE_SCHEMA_VERSION,
        _CUSTOM_TEMPLATES_SCHEMA_VERSION,
    }:
        raise ManagedStateError(
            f"unsupported managed-state schema version: {source_schema_version}"
        )
    _known_keys(
        root,
        {
            "schema_version",
            "playlists",
            "sources",
            "source_overrides",
            "templates",
            "quarantined_templates",
        }
        if source_schema_version >= _CUSTOM_TEMPLATES_SCHEMA_VERSION
        else {"schema_version", "playlists", "sources", "source_overrides"}
        if source_schema_version >= 5
        else {"schema_version", "playlists", "sources"}
        if source_schema_version >= 3
        else {"schema_version", "playlists"},
        "managed state",
    )
    sources = (
        tuple(
            _parse_source(item, f"sources[{index}]", source_schema_version=source_schema_version)
            for index, item in enumerate(
                _sequence(_required(root, "sources", "managed state"), "sources")
            )
        )
        if source_schema_version >= 3
        else ()
    )
    overrides = []
    if source_schema_version >= 5:
        for index, value in enumerate(
            _sequence(_required(root, "source_overrides", "managed state"), "source_overrides")
        ):
            path = f"source_overrides[{index}]"
            data = _mapping(value, path)
            _known_keys(data, {"source_id", "public_release_mode"}, path)
            overrides.append(
                ManagedSourceOverride(
                    SourceId(_string(_required(data, "source_id", path), f"{path}.source_id")),
                    _public_release_mode(_required(data, "public_release_mode", path)),
                )
            )
    sorted_overrides = tuple(sorted(overrides, key=lambda item: str(item.source_id)))
    _validate_overrides(sorted_overrides, BUILTIN_CATALOG)
    seen_source_ids: set[SourceId] = set()
    seen_show_ids: set[str] = set()
    for index, source in enumerate(sources):
        if source.id in seen_source_ids:
            raise ManagedStateError(f"sources[{index}].id: duplicate custom source id {source.id}")
        if source.spotify_show_id in seen_show_ids:
            raise ManagedStateError(
                f"sources[{index}].spotify_show_id: duplicate custom Spotify show id"
            )
        seen_source_ids.add(source.id)
        seen_show_ids.add(source.spotify_show_id)
    raw_playlists = _sequence(_required(root, "playlists", "managed state"), "playlists")
    playlists = tuple(
        _parse_playlist(
            item,
            f"playlists[{index}]",
            source_schema_version=source_schema_version,
        )
        for index, item in enumerate(raw_playlists)
    )
    ids = [playlist.id for playlist in playlists]
    if len(ids) != len(set(ids)):
        raise ManagedStateError("managed playlist ids must be unique")

    seen_destinations: set[tuple[AdapterId, str]] = set()
    for playlist in playlists:
        if playlist.enabled and not playlist.source_ids:
            raise ManagedStateError(f"enabled managed playlist {playlist.id} must select a source")
        destination_key = (
            playlist.destination.adapter_id,
            playlist.destination.external_id,
        )
        if playlist.enabled and destination_key in seen_destinations:
            raise ManagedStateError(
                f"duplicate enabled destination for managed playlist {playlist.id}"
            )
        if playlist.enabled:
            seen_destinations.add(destination_key)

    # Schema v6 remains on disk until the operator explicitly creates a custom draft.
    # Merely deploying a newer image must not make a v6 rollback impossible.
    migrated_from = (
        source_schema_version if source_schema_version < _MANAGED_STATE_SCHEMA_VERSION else None
    )
    sources = tuple(sorted(sources, key=lambda source: str(source.id)))
    source_definitions = effective_sources(
        BUILTIN_CATALOG,
        ManagedState(sources=sources, source_overrides=sorted_overrides),
    )
    templates: list[LocalPlaylistTemplate] = []
    template_positions: list[int] = []
    quarantined: list[QuarantinedPlaylistTemplate] = []
    if source_schema_version >= _CUSTOM_TEMPLATES_SCHEMA_VERSION:
        raw_templates = _sequence(_required(root, "templates", "managed state"), "templates")
        for index, raw in enumerate(raw_templates):
            try:
                draft = _parse_local_template(raw, f"templates[{index}]")
                effective_playlist_templates(BUILTIN_CATALOG, (draft,), sources=source_definitions)
            except ManagedStateError, LocalPlaylistTemplateError:
                quarantined.append(_quarantined(raw, index, "invalid_definition"))
            else:
                templates.append(draft)
                template_positions.append(index)
        raw_quarantine = _sequence(root.get("quarantined_templates", []), "quarantined_templates")
        for index, raw in enumerate(raw_quarantine):
            item = _mapping(raw, f"quarantined_templates[{index}]")
            _known_keys(item, {"source_index", "reason", "raw"}, f"quarantined_templates[{index}]")
            source_index = _integer(
                _required(item, "source_index", f"quarantined_templates[{index}]"),
                f"quarantined_templates[{index}].source_index",
            )
            if source_index < 0:
                raise ManagedStateError("quarantine source_index cannot be negative")
            reason = _string(
                _required(item, "reason", f"quarantined_templates[{index}]"),
                f"quarantined_templates[{index}].reason",
            )
            if reason not in {"invalid_definition", "duplicate_id"}:
                raise ManagedStateError("unknown quarantine reason")
            quarantined.append(
                _quarantined(
                    _required(item, "raw", f"quarantined_templates[{index}]"),
                    source_index,
                    reason,
                )
            )
    # Duplicate draft IDs are ambiguous: quarantine *all* contenders, not just
    # whichever one happened to appear second in JSON.
    id_counts: dict[str, int] = {}
    for template in templates:
        id_counts[str(template.id)] = id_counts.get(str(template.id), 0) + 1
    for invalid in quarantined:
        if invalid.template_id is not None:
            id_counts[invalid.template_id] = id_counts.get(invalid.template_id, 0) + 1
    accepted: list[LocalPlaylistTemplate] = []
    for template, index in zip(templates, template_positions, strict=True):
        if id_counts[str(template.id)] > 1:
            quarantined.append(
                _quarantined(_serialize_local_template(template), index, "duplicate_id")
            )
        else:
            accepted.append(template)
    state = ManagedState(
        schema_version=source_schema_version
        if source_schema_version >= _MANAGED_STATE_SCHEMA_VERSION
        else _MANAGED_STATE_SCHEMA_VERSION,
        sources=sources,
        playlists=playlists,
        source_overrides=sorted_overrides,
        templates=tuple(sorted(accepted, key=lambda item: str(item.id))),
        quarantined_templates=tuple(quarantined),
    )
    return state, migrated_from


def serialize_managed_state(state: ManagedState) -> dict[str, object]:
    if state.schema_version not in {
        _MANAGED_STATE_SCHEMA_VERSION,
        _CUSTOM_TEMPLATES_SCHEMA_VERSION,
    }:
        raise ManagedStateError(f"unsupported managed-state schema version: {state.schema_version}")
    if state.schema_version == _MANAGED_STATE_SCHEMA_VERSION and (
        state.templates or state.quarantined_templates
    ):
        raise ManagedStateError("custom playlist templates require managed-state schema v7")
    _validate_overrides(state.source_overrides, BUILTIN_CATALOG)
    for index, source in enumerate(state.sources):
        _validate_source(source, f"sources[{index}]")
    serialized_playlists: list[dict[str, object]] = []
    for playlist in state.playlists:
        item: dict[str, object] = {
            "id": str(playlist.id),
            "template_id": str(playlist.template_id),
            "enabled": playlist.enabled,
            "display_name": playlist.display_name,
            "description": playlist.description,
            "cover_id": playlist.cover_id,
            "source_ids": [str(source_id) for source_id in playlist.source_ids],
            "destination": {
                "adapter_id": str(playlist.destination.adapter_id),
                "external_id": playlist.destination.external_id,
            },
            "retention_hours": playlist.retention_hours,
            "max_episodes": playlist.max_episodes,
            "max_duration_seconds": playlist.max_duration_seconds,
            "min_duration_seconds": playlist.min_duration_seconds,
        }
        serialized_playlists.append(item)
    sorted_sources = tuple(sorted(state.sources, key=lambda source: str(source.id)))
    payload: dict[str, object] = {
        "schema_version": state.schema_version,
        "sources": [_serialize_source(source) for source in sorted_sources],
        "source_overrides": [
            {
                "source_id": str(item.source_id),
                "public_release_mode": item.public_release_mode.value,
            }
            for item in sorted(state.source_overrides, key=lambda item: str(item.source_id))
        ],
        "playlists": serialized_playlists,
    }
    if state.schema_version == _CUSTOM_TEMPLATES_SCHEMA_VERSION:
        sorted_templates = tuple(sorted(state.templates, key=lambda item: str(item.id)))
        try:
            effective_playlist_templates(
                BUILTIN_CATALOG, sorted_templates, sources=effective_sources(BUILTIN_CATALOG, state)
            )
        except LocalPlaylistTemplateError as exc:
            raise ManagedStateError(str(exc)) from exc
        payload["templates"] = [_serialize_local_template(item) for item in sorted_templates]
        if state.quarantined_templates:
            payload["quarantined_templates"] = [
                {
                    "source_index": item.source_index,
                    "reason": item.reason,
                    "raw": json.loads(item.raw_json),
                }
                for item in state.quarantined_templates
            ]
    canonical = parse_managed_state(payload)
    if canonical != replace(
        state,
        sources=sorted_sources,
        templates=tuple(sorted(state.templates, key=lambda item: str(item.id))),
        source_overrides=tuple(
            sorted(state.source_overrides, key=lambda item: str(item.source_id))
        ),
    ):
        raise ManagedStateError("managed state contains non-canonical values")
    return payload


def _serialize_local_template(item: LocalPlaylistTemplate) -> dict[str, object]:
    return {
        "id": str(item.id),
        "display_name": item.display_name,
        "description": item.description,
        "default_source_ids": [str(value) for value in item.default_source_ids],
        "cover_id": item.cover_id,
        "countries": [str(value) for value in item.countries],
        "languages": [str(value) for value in item.languages],
        "retention_hours": item.retention_hours,
        "max_episodes": item.max_episodes,
        "min_duration_seconds": item.min_duration_seconds,
        "max_duration_seconds": item.max_duration_seconds,
        "ordering": item.ordering.value,
        "hidden": item.hidden,
    }


def _parse_local_template(value: object, path: str) -> LocalPlaylistTemplate:
    data = _mapping(value, path)
    _known_keys(
        data,
        {
            "id",
            "display_name",
            "description",
            "default_source_ids",
            "cover_id",
            "countries",
            "languages",
            "retention_hours",
            "max_episodes",
            "min_duration_seconds",
            "max_duration_seconds",
            "ordering",
            "hidden",
        },
        path,
    )
    source_values = _sequence(
        _required(data, "default_source_ids", path), f"{path}.default_source_ids"
    )
    countries = _sequence(_required(data, "countries", path), f"{path}.countries")
    languages = _sequence(_required(data, "languages", path), f"{path}.languages")
    ordering_name = _string(_required(data, "ordering", path), f"{path}.ordering")
    if ordering_name != OrderingPolicy.EDITION_AT_DESC.value:
        raise ManagedStateError(f"{path}.ordering: unsupported ordering")
    try:
        return LocalPlaylistTemplate(
            id=PlaylistId(_nonempty_string(_required(data, "id", path), f"{path}.id")),
            display_name=_string(_required(data, "display_name", path), f"{path}.display_name"),
            description=_string(_required(data, "description", path), f"{path}.description"),
            default_source_ids=tuple(
                SourceId(_string(item, f"{path}.default_source_ids[{index}]"))
                for index, item in enumerate(source_values)
            ),
            cover_id=_string(_required(data, "cover_id", path), f"{path}.cover_id"),
            countries=tuple(
                CountryCode(_string(item, f"{path}.countries[{index}]"))
                for index, item in enumerate(countries)
            ),
            languages=tuple(
                LanguageTag(_string(item, f"{path}.languages[{index}]"))
                for index, item in enumerate(languages)
            ),
            retention_hours=_integer(
                _required(data, "retention_hours", path), f"{path}.retention_hours"
            ),
            max_episodes=_integer(_required(data, "max_episodes", path), f"{path}.max_episodes"),
            min_duration_seconds=_integer(
                _required(data, "min_duration_seconds", path), f"{path}.min_duration_seconds"
            ),
            max_duration_seconds=_integer(
                _required(data, "max_duration_seconds", path), f"{path}.max_duration_seconds"
            ),
            ordering=OrderingPolicy.EDITION_AT_DESC,
            hidden=_boolean(_required(data, "hidden", path), f"{path}.hidden"),
        )
    except LocalPlaylistTemplateError as exc:
        raise ManagedStateError(f"{path}: {exc}") from exc


def _serialize_source(source: ManagedSource) -> dict[str, object]:
    return {
        "id": str(source.id),
        "spotify_show_id": source.spotify_show_id,
        "display_name": source.display_name,
        "origin": {"kind": source.origin.kind.value, "code": source.origin.code},
        "language": str(source.language),
        "editorial_scope": source.editorial_scope.value,
        "timezone": source.timezone,
        "parser_id": str(source.parser_id),
        "temporal_strategy": source.temporal_strategy.value,
        "public_release_mode": source.public_release_mode.value,
        "collection_filter_id": source.collection_filter_id,
        "enabled": source.enabled,
    }


def _parse_source(value: object, path: str, *, source_schema_version: int) -> ManagedSource:
    data = _mapping(value, path)
    _known_keys(
        data,
        {
            "id",
            "spotify_show_id",
            "display_name",
            "origin",
            "language",
            "editorial_scope",
            "timezone",
            "parser_id",
            "temporal_strategy",
            "public_release_mode",
            "collection_filter_id",
            "enabled",
        },
        path,
    )
    if source_schema_version < 5 and "public_release_mode" in data:
        raise ManagedStateError(f"{path}.public_release_mode: requires schema v5")
    origin_data = _mapping(_required(data, "origin", path), f"{path}.origin")
    _known_keys(origin_data, {"kind", "code"}, f"{path}.origin")
    try:
        kind_text = _string(_required(origin_data, "kind", f"{path}.origin"), f"{path}.origin.kind")
        origin = SourceOrigin(
            SourceOriginKind(kind_text),
            _string(_required(origin_data, "code", f"{path}.origin"), f"{path}.origin.code"),
        )
    except ValueError as exc:
        raise ManagedStateError(f"{path}.origin: {exc}") from exc
    scope_text = _string(_required(data, "editorial_scope", path), f"{path}.editorial_scope")
    try:
        scope = EditorialScope(scope_text)
    except ValueError as exc:
        raise ManagedStateError(
            f"{path}.editorial_scope: unsupported value {scope_text!r}"
        ) from exc
    source = ManagedSource(
        id=SourceId(_string(_required(data, "id", path), f"{path}.id")),
        spotify_show_id=_string(
            _required(data, "spotify_show_id", path), f"{path}.spotify_show_id"
        ),
        display_name=_nonempty_string(
            _required(data, "display_name", path), f"{path}.display_name"
        ),
        origin=origin,
        language=LanguageTag(_string(_required(data, "language", path), f"{path}.language")),
        editorial_scope=scope,
        timezone=_string(_required(data, "timezone", path), f"{path}.timezone"),
        parser_id=ParserId(_string(_required(data, "parser_id", path), f"{path}.parser_id")),
        temporal_strategy=_temporal_strategy(data.get("temporal_strategy", "auto")),
        public_release_mode=_public_release_mode(data.get("public_release_mode", "instant")),
        collection_filter_id=(
            None
            if data.get("collection_filter_id") is None
            else _string(data["collection_filter_id"], f"{path}.collection_filter_id")
        ),
        enabled=_boolean(_required(data, "enabled", path), f"{path}.enabled"),
    )
    _validate_source(source, path)
    return source


def _validate_source(source: ManagedSource, path: str) -> None:
    if not isinstance(source.spotify_show_id, str) or not is_spotify_id(source.spotify_show_id):
        raise ManagedStateError(f"{path}.spotify_show_id: expected a canonical Spotify show ID")
    if source.id != SourceId(f"custom_{source.spotify_show_id}"):
        raise ManagedStateError(f"{path}.id: must equal custom_<spotify_show_id>")
    if (
        not isinstance(source.display_name, str)
        or not 1 <= len(source.display_name) <= _MAX_SOURCE_DISPLAY_NAME_LENGTH
        or source.display_name != source.display_name.strip()
        or not source.display_name.isprintable()
    ):
        raise ManagedStateError(f"{path}.display_name: must be trimmed and 1-120 characters")
    if not isinstance(source.origin, SourceOrigin):
        raise ManagedStateError(f"{path}.origin: expected a valid source origin")
    if _SOURCE_LANGUAGE_TAG.fullmatch(str(source.language)) is None:
        raise ManagedStateError(f"{path}.language: expected a language tag such as en or es-ES")
    if not isinstance(source.editorial_scope, EditorialScope):
        raise ManagedStateError(f"{path}.editorial_scope: unsupported value")
    if (
        not isinstance(source.timezone, str)
        or not 1 <= len(source.timezone) <= _MAX_SOURCE_TIMEZONE_LENGTH
        or source.timezone != source.timezone.strip()
    ):
        raise ManagedStateError(f"{path}.timezone: expected a bounded IANA timezone")
    try:
        ZoneInfo(source.timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ManagedStateError(f"{path}.timezone: unknown IANA timezone") from exc
    if not isinstance(source.public_release_mode, PublicReleaseMode):
        raise ManagedStateError(f"{path}.public_release_mode: unknown mode")
    if not isinstance(source.temporal_strategy, TemporalStrategy):
        raise ManagedStateError(f"{path}.temporal_strategy: unknown strategy")
    if not custom_strategy_allowed(str(source.parser_id), None):
        raise ManagedStateError(f"{path}.parser_id: not allowed for custom sources")
    if not custom_strategy_allowed(str(source.parser_id), source.collection_filter_id):
        raise ManagedStateError(f"{path}.collection_filter_id: not allowed for custom sources")
    if not isinstance(source.enabled, bool):
        raise ManagedStateError(f"{path}.enabled: expected a boolean")


def _parse_playlist(
    value: object,
    path: str,
    *,
    source_schema_version: int,
) -> ManagedPlaylist:
    data = _mapping(value, path)
    _known_keys(
        data,
        {
            "id",
            "template_id",
            "enabled",
            "display_name",
            "description",
            "cover_id",
            "source_ids",
            "destination",
            "retention_hours",
            "max_episodes",
            "max_duration_seconds",
            "min_duration_seconds",
        },
        path,
    )
    source_values = _sequence(_required(data, "source_ids", path), f"{path}.source_ids")
    source_ids = tuple(
        SourceId(_nonempty_string(item, f"{path}.source_ids[{index}]"))
        for index, item in enumerate(source_values)
    )
    if len(source_ids) != len(set(source_ids)):
        raise ManagedStateError(f"{path}.source_ids contains duplicates")
    destination_data = _mapping(_required(data, "destination", path), f"{path}.destination")
    _known_keys(destination_data, {"adapter_id", "external_id"}, f"{path}.destination")
    retention_hours = _positive_integer(
        _required(data, "retention_hours", path), f"{path}.retention_hours"
    )
    max_episodes = _positive_integer(_required(data, "max_episodes", path), f"{path}.max_episodes")

    if source_schema_version == _LEGACY_MANAGED_STATE_SCHEMA_VERSION:
        raw_max_duration = data.get("max_duration_seconds")
        max_duration_seconds = (
            _LEGACY_V1_DEFAULT_MAX_DURATION_SECONDS
            if raw_max_duration is None
            else _duration_seconds(raw_max_duration, f"{path}.max_duration_seconds")
        )
    else:
        max_duration_seconds = _duration_seconds(
            _required(data, "max_duration_seconds", path),
            f"{path}.max_duration_seconds",
        )

    try:
        min_duration_seconds = validate_persisted_min_duration_seconds(
            _required(data, "min_duration_seconds", path)
            if source_schema_version >= 6
            else data.get("min_duration_seconds", 0)
        )
        validate_duration_range(min_duration_seconds, max_duration_seconds)
    except ValueError as exc:
        raise ManagedStateError(f"{path}: {exc}") from exc

    return ManagedPlaylist(
        id=PlaylistId(_nonempty_string(_required(data, "id", path), f"{path}.id")),
        template_id=PlaylistId(
            _nonempty_string(_required(data, "template_id", path), f"{path}.template_id")
        ),
        enabled=_boolean(_required(data, "enabled", path), f"{path}.enabled"),
        display_name=_nonempty_string(
            _required(data, "display_name", path), f"{path}.display_name"
        ),
        description=_string(_required(data, "description", path), f"{path}.description"),
        cover_id=_nonempty_string(_required(data, "cover_id", path), f"{path}.cover_id"),
        source_ids=source_ids,
        destination=DestinationReference(
            adapter_id=AdapterId(
                _nonempty_string(
                    _required(destination_data, "adapter_id", f"{path}.destination"),
                    f"{path}.destination.adapter_id",
                )
            ),
            external_id=_nonempty_string(
                _required(destination_data, "external_id", f"{path}.destination"),
                f"{path}.destination.external_id",
            ),
        ),
        retention_hours=retention_hours,
        max_episodes=max_episodes,
        max_duration_seconds=max_duration_seconds,
        min_duration_seconds=min_duration_seconds,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManagedStateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ManagedStateError(f"{path}: expected an object")
    return cast(Mapping[str, object], value)


def _sequence(value: object, path: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ManagedStateError(f"{path}: expected an array")
    return cast(Sequence[object], value)


def _required(data: Mapping[str, object], key: str, path: str) -> object:
    if key not in data:
        raise ManagedStateError(f"{path}.{key}: required")
    return data[key]


def _known_keys(data: Mapping[str, object], allowed: set[str], path: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ManagedStateError(f"{path}: unknown key {sorted(unknown)[0]!r}")


def _string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ManagedStateError(f"{path}: expected a string")
    return value


def _nonempty_string(value: object, path: str) -> str:
    result = _string(value, path).strip()
    if not result:
        raise ManagedStateError(f"{path}: must not be empty")
    return result


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ManagedStateError(f"{path}: expected a boolean")
    return value


def _integer(value: object, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ManagedStateError(f"{path}: expected an integer")
    return value


def _positive_integer(value: object, path: str) -> int:
    result = _integer(value, path)
    if result <= 0:
        raise ManagedStateError(f"{path}: must be positive")
    return result


def _duration_seconds(value: object, path: str) -> int:
    try:
        return validate_persisted_duration_seconds(value)
    except ValueError as exc:
        raise ManagedStateError(f"{path}: must be positive") from exc
