from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml  # type: ignore[import-untyped]
from yaml.constructor import ConstructorError  # type: ignore[import-untyped]

from news_bulletin_playlist.models import (
    AdapterId,
    CountryCode,
    DestinationReference,
    EngineConfig,
    ExternalReference,
    LanguageTag,
    OrderingPolicy,
    ParserId,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.registry import has_title_parser

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_YAML_BOOLEAN_RE = re.compile(r"^(?:true|false)$", re.IGNORECASE)


class ConfigError(ValueError):
    """An actionable configuration error tied to a YAML path."""


class _UniqueKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    pass


# PyYAML follows YAML 1.1 boolean resolution by default, where unquoted values
# such as NO/no/ON/OFF become booleans. That breaks valid country/language codes
# (notably Norway: NO / no). Give this loader YAML-1.2-like boolean semantics:
# only true/false are implicit booleans.
_UniqueKeySafeLoader.yaml_implicit_resolvers = {
    key: list(resolvers) for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _resolver_key, _resolvers in _UniqueKeySafeLoader.yaml_implicit_resolvers.items():
    _UniqueKeySafeLoader.yaml_implicit_resolvers[_resolver_key] = [
        resolver for resolver in _resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]
_UniqueKeySafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    _YAML_BOOLEAN_RE,
    list("tTfF"),
)


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_config(path: Path) -> EngineConfig:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_config(raw, origin=str(path))


def parse_config(payload: object, *, origin: str = "<memory>") -> EngineConfig:
    root = _mapping(payload, origin)
    _known_keys(root, {"schema_version", "sources", "playlists"}, origin)
    schema_version = _integer(_required(root, "schema_version", origin), f"{origin}.schema_version")
    if schema_version != 1:
        raise ConfigError(f"{origin}.schema_version: unsupported version {schema_version}")

    sources_raw = _sequence(_required(root, "sources", origin), f"{origin}.sources")
    playlists_raw = _sequence(_required(root, "playlists", origin), f"{origin}.playlists")
    sources = tuple(
        _parse_source(item, f"{origin}.sources[{index}]") for index, item in enumerate(sources_raw)
    )
    playlists = tuple(
        _parse_playlist(item, f"{origin}.playlists[{index}]")
        for index, item in enumerate(playlists_raw)
    )
    _validate_unique_ids(sources, playlists, origin)
    _validate_playlist_sources(sources, playlists, origin)
    _validate_unique_enabled_destinations(playlists, origin)
    return EngineConfig(schema_version=schema_version, sources=sources, playlists=playlists)


def _parse_source(value: object, path: str) -> SourceDefinition:
    data = _mapping(value, path)
    _known_keys(
        data,
        {
            "id",
            "display_name",
            "countries",
            "languages",
            "timezone",
            "enabled",
            "parser_id",
            "endpoint_url",
            "external_references",
        },
        path,
    )
    source_id = SourceId(_identifier(_required(data, "id", path), f"{path}.id"))
    parser_id_value = _identifier(_required(data, "parser_id", path), f"{path}.parser_id")
    if not has_title_parser(parser_id_value):
        raise ConfigError(f"{path}.parser_id: unknown parser {parser_id_value!r}")
    timezone_name = _string(_required(data, "timezone", path), f"{path}.timezone")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"{path}.timezone: unknown IANA timezone {timezone_name!r}") from exc
    endpoint = data.get("endpoint_url")
    endpoint_url = None if endpoint is None else _http_url(endpoint, f"{path}.endpoint_url")
    references_raw = _sequence(data.get("external_references", ()), f"{path}.external_references")
    references = tuple(
        _parse_external_reference(item, f"{path}.external_references[{index}]")
        for index, item in enumerate(references_raw)
    )
    if len({(ref.system, ref.resource_type, ref.external_id) for ref in references}) != len(
        references
    ):
        raise ConfigError(f"{path}.external_references: duplicate reference")
    return SourceDefinition(
        id=source_id,
        display_name=_nonempty_string(
            _required(data, "display_name", path), f"{path}.display_name"
        ),
        countries=_countries(_required(data, "countries", path), f"{path}.countries"),
        languages=_languages(_required(data, "languages", path), f"{path}.languages"),
        timezone=timezone,
        enabled=_boolean(_required(data, "enabled", path), f"{path}.enabled"),
        parser_id=ParserId(parser_id_value),
        endpoint_url=endpoint_url,
        external_references=references,
    )


def _parse_external_reference(value: object, path: str) -> ExternalReference:
    data = _mapping(value, path)
    _known_keys(data, {"system", "resource_type", "external_id"}, path)
    return ExternalReference(
        system=_identifier(_required(data, "system", path), f"{path}.system"),
        resource_type=_identifier(_required(data, "resource_type", path), f"{path}.resource_type"),
        external_id=_nonempty_string(_required(data, "external_id", path), f"{path}.external_id"),
    )


def _parse_playlist(value: object, path: str) -> PlaylistDefinition:
    data = _mapping(value, path)
    _known_keys(
        data,
        {
            "id",
            "display_name",
            "description",
            "countries",
            "languages",
            "enabled",
            "source_selection",
            "destination",
            "retention_hours",
            "max_episodes",
            "ordering",
        },
        path,
    )
    selection_data = _mapping(_required(data, "source_selection", path), f"{path}.source_selection")
    _known_keys(selection_data, {"explicit"}, f"{path}.source_selection")
    explicit_raw = _sequence(
        _required(selection_data, "explicit", f"{path}.source_selection"),
        f"{path}.source_selection.explicit",
    )
    explicit = tuple(
        SourceId(_identifier(item, f"{path}.source_selection.explicit[{index}]"))
        for index, item in enumerate(explicit_raw)
    )
    if len(set(explicit)) != len(explicit):
        raise ConfigError(f"{path}.source_selection.explicit: duplicate source id")
    enabled = _boolean(_required(data, "enabled", path), f"{path}.enabled")
    if enabled and not explicit:
        raise ConfigError(
            f"{path}.source_selection.explicit: enabled playlist must select a source"
        )
    ordering_raw = data.get("ordering", OrderingPolicy.PUBLISHED_AT_DESC.value)
    ordering_text = _string(ordering_raw, f"{path}.ordering")
    try:
        ordering = OrderingPolicy(ordering_text)
    except ValueError as exc:
        raise ConfigError(f"{path}.ordering: unsupported policy {ordering_text!r}") from exc
    retention_hours = _positive_integer(data.get("retention_hours", 48), f"{path}.retention_hours")
    max_episodes = _positive_integer(data.get("max_episodes", 100), f"{path}.max_episodes")
    return PlaylistDefinition(
        id=PlaylistId(_identifier(_required(data, "id", path), f"{path}.id")),
        display_name=_nonempty_string(
            _required(data, "display_name", path), f"{path}.display_name"
        ),
        description=_string(_required(data, "description", path), f"{path}.description"),
        countries=_countries(_required(data, "countries", path), f"{path}.countries"),
        languages=_languages(_required(data, "languages", path), f"{path}.languages"),
        enabled=enabled,
        source_selection=SourceSelection(explicit=explicit),
        destination=_parse_destination(_required(data, "destination", path), f"{path}.destination"),
        retention_hours=retention_hours,
        max_episodes=max_episodes,
        ordering=ordering,
    )


def _parse_destination(value: object, path: str) -> DestinationReference:
    data = _mapping(value, path)
    _known_keys(data, {"adapter_id", "external_id"}, path)
    return DestinationReference(
        adapter_id=AdapterId(
            _identifier(_required(data, "adapter_id", path), f"{path}.adapter_id")
        ),
        external_id=_nonempty_string(_required(data, "external_id", path), f"{path}.external_id"),
    )


def _validate_unique_ids(
    sources: tuple[SourceDefinition, ...], playlists: tuple[PlaylistDefinition, ...], origin: str
) -> None:
    source_ids = [source.id for source in sources]
    if len(set(source_ids)) != len(source_ids):
        raise ConfigError(f"{origin}.sources: duplicate source id")
    playlist_ids = [playlist.id for playlist in playlists]
    if len(set(playlist_ids)) != len(playlist_ids):
        raise ConfigError(f"{origin}.playlists: duplicate playlist id")


def _validate_playlist_sources(
    sources: tuple[SourceDefinition, ...], playlists: tuple[PlaylistDefinition, ...], origin: str
) -> None:
    by_id = {source.id: source for source in sources}
    for playlist_index, playlist in enumerate(playlists):
        for source_index, source_id in enumerate(playlist.source_selection.explicit):
            path = f"{origin}.playlists[{playlist_index}].source_selection.explicit[{source_index}]"
            source = by_id.get(source_id)
            if source is None:
                raise ConfigError(f"{path}: unknown source {source_id!r}")
            if playlist.enabled and not source.enabled:
                raise ConfigError(
                    f"{path}: enabled playlist references disabled source {source_id!r}"
                )


def _validate_unique_enabled_destinations(
    playlists: tuple[PlaylistDefinition, ...], origin: str
) -> None:
    seen: dict[tuple[AdapterId, str], int] = {}
    for playlist_index, playlist in enumerate(playlists):
        if not playlist.enabled:
            continue
        key = (playlist.destination.adapter_id, playlist.destination.external_id)
        previous_index = seen.get(key)
        if previous_index is not None:
            raise ConfigError(
                f"{origin}.playlists[{playlist_index}].destination: duplicate enabled destination; "
                f"already used by {origin}.playlists[{previous_index}]"
            )
        seen[key] = playlist_index


def _countries(value: object, path: str) -> tuple[CountryCode, ...]:
    items = _sequence(value, path)
    if not items:
        raise ConfigError(f"{path}: must not be empty")
    result: list[CountryCode] = []
    for index, item in enumerate(items):
        country = _string(item, f"{path}[{index}]")
        if (
            len(country) != 2
            or not country.isascii()
            or not country.isalpha()
            or not country.isupper()
        ):
            raise ConfigError(f"{path}[{index}]: expected two uppercase ASCII letters")
        result.append(CountryCode(country))
    return tuple(result)


def _languages(value: object, path: str) -> tuple[LanguageTag, ...]:
    items = _sequence(value, path)
    if not items:
        raise ConfigError(f"{path}: must not be empty")
    result: list[LanguageTag] = []
    for index, item in enumerate(items):
        language = _string(item, f"{path}[{index}]")
        if not _LANGUAGE_RE.fullmatch(language):
            raise ConfigError(f"{path}[{index}]: expected a conservative BCP-47 language tag")
        result.append(LanguageTag(language))
    return tuple(result)


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{path}: expected a mapping")
    return cast(Mapping[str, object], value)


def _sequence(value: object, path: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ConfigError(f"{path}: expected a list")
    return cast(Sequence[object], value)


def _known_keys(data: Mapping[str, object], allowed: set[str], path: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"{path}: unknown key {sorted(unknown)[0]!r}")


def _required(data: Mapping[str, object], key: str, path: str) -> object:
    if key not in data:
        raise ConfigError(f"{path}.{key}: required")
    return data[key]


def _string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path}: expected a string")
    return value


def _nonempty_string(value: object, path: str) -> str:
    result = _string(value, path)
    if not result.strip():
        raise ConfigError(f"{path}: must not be empty")
    return result


def _identifier(value: object, path: str) -> str:
    result = _string(value, path)
    if not _ID_RE.fullmatch(result):
        raise ConfigError(f"{path}: expected [a-z0-9][a-z0-9_-]*")
    return result


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{path}: expected a boolean")
    return value


def _integer(value: object, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{path}: expected an integer")
    return value


def _positive_integer(value: object, path: str) -> int:
    result = _integer(value, path)
    if result <= 0:
        raise ConfigError(f"{path}: must be positive")
    return result


def _http_url(value: object, path: str) -> str:
    result = _nonempty_string(value, path)
    parsed = urlparse(result)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{path}: expected an HTTP(S) URL")
    return result
