"""Persistent per-installation choices layered over the built-in catalog."""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.models import (
    AdapterId,
    DestinationReference,
    DurationPolicy,
    EngineConfig,
    PlaylistDefinition,
    PlaylistId,
    SourceId,
    SourceSelection,
)

MANAGED_STATE_FILENAME = "managed-state.json"
_MANAGED_STATE_SCHEMA_VERSION = 2
_LEGACY_MANAGED_STATE_SCHEMA_VERSION = 1
_LEGACY_V1_DEFAULT_MAX_DURATION_SECONDS = 1800


class ManagedStateError(ValueError):
    """Actionable error for invalid or unsafe managed installation state."""


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


@dataclass(frozen=True, slots=True)
class ManagedState:
    schema_version: int = _MANAGED_STATE_SCHEMA_VERSION
    playlists: tuple[ManagedPlaylist, ...] = ()


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
            _fsync_directory(parent)
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
    )


def compile_engine_config(catalog: BuiltInCatalog, state: ManagedState) -> EngineConfig:
    """Compile immutable catalog knowledge plus local choices into the existing engine model."""
    source_ids = {source.id for source in catalog.sources}
    templates = {template.id: template for template in catalog.playlists}
    playlists: list[PlaylistDefinition] = []
    seen_ids: set[PlaylistId] = set()
    seen_destinations: set[tuple[AdapterId, str]] = set()

    for managed in state.playlists:
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
        if managed.enabled and not managed.source_ids:
            raise ManagedStateError(f"enabled managed playlist {managed.id} must select a source")
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
        if max_duration_seconds <= 0:
            raise ManagedStateError(
                f"managed playlist {managed.id} max_duration_seconds must be positive"
            )
        duration_policy = DurationPolicy(
            default_max_seconds=max_duration_seconds,
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
                source_selection=SourceSelection(explicit=managed.source_ids),
                destination=managed.destination,
                retention_hours=managed.retention_hours,
                max_episodes=managed.max_episodes,
                ordering=template.ordering,
                duration_policy=duration_policy,
            )
        )

    return EngineConfig(
        schema_version=1,
        sources=catalog.sources,
        playlists=tuple(playlists),
    )


def parse_managed_state(payload: object) -> ManagedState:
    """Parse current or legacy managed state into the current in-memory schema."""
    state, _ = _parse_managed_state_with_version(payload)
    return state


def _parse_managed_state_with_version(payload: object) -> tuple[ManagedState, int | None]:
    root = _mapping(payload, "managed state")
    _known_keys(root, {"schema_version", "playlists"}, "managed state")
    source_schema_version = _integer(
        _required(root, "schema_version", "managed state"), "schema_version"
    )
    if source_schema_version not in {
        _LEGACY_MANAGED_STATE_SCHEMA_VERSION,
        _MANAGED_STATE_SCHEMA_VERSION,
    }:
        raise ManagedStateError(
            f"unsupported managed-state schema version: {source_schema_version}"
        )
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

    migrated_from = (
        source_schema_version
        if source_schema_version != _MANAGED_STATE_SCHEMA_VERSION
        else None
    )
    return (
        ManagedState(schema_version=_MANAGED_STATE_SCHEMA_VERSION, playlists=playlists),
        migrated_from,
    )


def serialize_managed_state(state: ManagedState) -> dict[str, object]:
    if state.schema_version != _MANAGED_STATE_SCHEMA_VERSION:
        raise ManagedStateError(f"unsupported managed-state schema version: {state.schema_version}")
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
        }
        serialized_playlists.append(item)
    payload: dict[str, object] = {
        "schema_version": state.schema_version,
        "playlists": serialized_playlists,
    }
    canonical = parse_managed_state(payload)
    if canonical != state:
        raise ManagedStateError("managed state contains non-canonical values")
    return payload


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
            else _positive_integer(raw_max_duration, f"{path}.max_duration_seconds")
        )
    else:
        max_duration_seconds = _positive_integer(
            _required(data, "max_duration_seconds", path),
            f"{path}.max_duration_seconds",
        )

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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
