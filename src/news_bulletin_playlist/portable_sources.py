"""Portable source configuration v1: pure, bounded JSON; never installation or backup.

Callers supply effective SourceDefinitions from one configuration snapshot. Decoding
preserves original logical IDs for future remapping; it does not authorize persistence,
activation, or the imported claim of built-in provenance.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from news_bulletin_playlist.models import (
    CountryCode,
    EditorialScope,
    ExternalReference,
    LanguageTag,
    ParserId,
    PublicReleaseMode,
    SourceDefinition,
    SourceId,
    SourceOrigin,
    SourceOriginKind,
    TemporalStrategy,
)
from news_bulletin_playlist.registry import has_collection_filter, has_title_parser
from news_bulletin_playlist.spotify.identifiers import is_spotify_id

SOURCE_FORMAT = "news-bulletin-playlist.sources"
SOURCE_FORMAT_VERSION = 1
MAX_SOURCE_DOCUMENT_BYTES = 256 * 1024
MAX_PORTABLE_SOURCES = 100
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-(?:[A-Z]{2}|[0-9]{3}))?\Z")
_COUNTRY = re.compile(r"[A-Z]{2}\Z")
# Explicit interchange contracts, not automatic permission for every future registry entry.
# Version 1 denotes the installed contract, not an immutable parser implementation.
_PARSER_V1 = frozenset(
    {
        "ser",
        "rne",
        "ondacero",
        "cnn",
        "cope",
        "rfi_journal_monde",
        "rmf_fakty",
        "release_date_title",
    }
)
_FILTER_V1 = frozenset({"un_news_es_minutes"})
_TEMPORAL_V1 = frozenset(
    {
        "auto",
        "full_datetime",
        "clock_catalogue_day",
        "partial_date_catalogue_day",
        "explicit_utc",
        "disabled",
    }
)
_PUBLIC_V1 = frozenset({"instant", "wall_time", "disabled"})
_SOURCE_KEYS = {
    "original_id",
    "spotify_show_id",
    "display_name",
    "origin",
    "countries",
    "languages",
    "editorial_scope",
    "timezone",
    "enabled",
    "provenance",
    "processing",
}
_PROCESSING_KEYS = {"parser", "temporal_strategy", "public_release_mode", "collection_filter"}


class PortableSourceError(ValueError):
    """Safe error: messages never interpolate untrusted keys, values or parser errors."""


class UnsupportedSourceCapability(PortableSourceError):
    """Well-shaped capability cannot be reproduced by this installation."""


@dataclass(frozen=True, slots=True)
class PortableSource:
    source: SourceDefinition
    provenance: str


@dataclass(frozen=True, slots=True)
class UnsupportedCapability:
    """Allowlisted diagnostic labels, never raw imported capability values."""

    category: str
    reason: str


@dataclass(frozen=True, slots=True)
class UnsupportedPortableSource:
    """Preview identity only; deliberately not an executable SourceDefinition."""

    id: SourceId
    display_name: str
    spotify_show_id: str
    issues: tuple[UnsupportedCapability, ...]


@dataclass(frozen=True, slots=True)
class _SourceMetadata:
    id: SourceId
    spotify_show_id: str
    display_name: str
    provenance: str
    origin: SourceOrigin
    editorial_scope: EditorialScope
    timezone: ZoneInfo
    countries: tuple[CountryCode, ...]
    languages: tuple[LanguageTag, ...]
    enabled: bool


def export_sources(
    sources: Sequence[SourceDefinition], *, built_in_ids: Collection[SourceId] = ()
) -> bytes:
    """Export one or many effective definitions, including disabled ones, without I/O.

    built_in_ids supplies informational provenance only; no catalogue is loaded implicitly.
    Unsupported external references fail instead of being silently dropped.
    """
    if not 1 <= len(sources) <= MAX_PORTABLE_SOURCES:
        raise PortableSourceError("Expected between 1 and 100 sources")
    return serialize_sources(
        tuple(
            PortableSource(source, "built_in" if source.id in built_in_ids else "custom")
            for source in sources
        )
    )


def serialize_sources(sources: Sequence[PortableSource]) -> bytes:
    """Produce deterministic UTF-8 bytes, with the same validation as the decoder."""
    if not 1 <= len(sources) <= MAX_PORTABLE_SOURCES:
        raise PortableSourceError("Expected between 1 and 100 sources")
    rows = [_source_payload(item) for item in sources]
    _parse_rows(rows)
    rows.sort(key=lambda row: (str(row["spotify_show_id"]), str(row["original_id"])))
    document = {
        "format": SOURCE_FORMAT,
        "version": SOURCE_FORMAT_VERSION,
        "sources": rows,
    }
    encoded = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_SOURCE_DOCUMENT_BYTES:
        raise PortableSourceError("Source document exceeds 256 KiB")
    return encoded


def parse_sources(document: bytes) -> tuple[PortableSource, ...]:
    """Validate a complete source document without installing or changing anything."""
    return _parse_rows(_source_document(document))


def _source_document(document: bytes) -> object:
    if not isinstance(document, bytes) or not 0 < len(document) <= MAX_SOURCE_DOCUMENT_BYTES:
        raise PortableSourceError("Expected a UTF-8 source document of at most 256 KiB")
    try:
        payload = json.loads(
            document.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_bad_constant
        )
    except ValueError, UnicodeError, RecursionError:
        raise PortableSourceError("Invalid source JSON document") from None
    root = _object(payload, {"format", "version", "sources"})
    if root["format"] != SOURCE_FORMAT:
        raise PortableSourceError("Unknown source document format")
    if type(root["version"]) is not int or root["version"] != SOURCE_FORMAT_VERSION:
        raise PortableSourceError("Unsupported source document version")
    return root["sources"]


def inspect_sources(document: bytes) -> tuple[PortableSource | UnsupportedPortableSource, ...]:
    """Preview only: validate the whole document and diagnose unsupported capabilities.

    Invalid metadata, shapes/types, identities and duplicates still reject the entire
    document. Only well-formed unsupported processing contracts become diagnostics.
    These records cannot authorize installation: apply must always use parse_sources.
    """
    rows = _source_rows(_source_document(document))
    result = tuple(_inspect_source(row) for row in rows)
    identities = [
        (item.source.id, item.source.external_references[0].external_id)
        if isinstance(item, PortableSource)
        else (item.id, item.spotify_show_id)
        for item in result
    ]
    _unique_identities(identities)
    return result


def _inspect_source(value: object) -> PortableSource | UnsupportedPortableSource:
    data = _object(value, _SOURCE_KEYS)
    metadata = _source_metadata(data)
    processing = _object(data["processing"], _PROCESSING_KEYS)
    issues: list[UnsupportedCapability] = []
    for key, category, supported in (
        ("parser", "Parser", _PARSER_V1),
        ("temporal_strategy", "Temporal strategy", _TEMPORAL_V1),
        ("public_release_mode", "Public release policy", _PUBLIC_V1),
        ("collection_filter", "Collection filter", _FILTER_V1),
    ):
        if key == "collection_filter" and processing[key] is None:
            continue
        identifier, version, parameters = _capability_shape(processing[key])
        # Continue validating all remaining capability shapes even after finding an
        # unsupported one: a later malformed capability must not become a preview row.
        reason = None
        if identifier not in supported:
            reason = "Unknown capability"
        elif version != 1:
            reason = "Unsupported version"
        elif parameters:
            reason = "Unsupported parameters"
        elif (key == "parser" and not has_title_parser(identifier)) or (
            key == "collection_filter" and not has_collection_filter(identifier)
        ):
            reason = "Capability not installed"
        if reason is not None:
            issues.append(UnsupportedCapability(category, reason))
    if issues:
        return UnsupportedPortableSource(
            metadata.id, metadata.display_name, metadata.spotify_show_id, tuple(issues)
        )
    return _parse_source(value)


def _source_payload(item: PortableSource) -> dict[str, object]:
    source = item.source
    references = source.external_references
    if len(references) != 1 or (references[0].system, references[0].resource_type) != (
        "spotify",
        "show",
    ):
        raise PortableSourceError("Expected exactly one canonical Spotify show reference")
    return {
        "original_id": source.id,
        "spotify_show_id": references[0].external_id,
        "display_name": source.display_name,
        "origin": {"kind": source.origin.kind.value, "code": source.origin.code},
        "countries": list(source.countries),
        "languages": list(source.languages),
        "editorial_scope": source.editorial_scope.value,
        "timezone": source.timezone.key,
        "enabled": source.enabled,
        "provenance": item.provenance,
        "processing": {
            "parser": _capability_payload(source.parser_id),
            "temporal_strategy": _capability_payload(source.temporal_strategy.value),
            "public_release_mode": _capability_payload(source.public_release_mode.value),
            "collection_filter": (
                None
                if source.collection_filter_id is None
                else _capability_payload(source.collection_filter_id)
            ),
        },
    }


def _capability_payload(identifier: str) -> dict[str, object]:
    return {"id": identifier, "version": 1, "parameters": {}}


def _source_rows(value: object) -> list[object]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PORTABLE_SOURCES:
        raise PortableSourceError("Expected between 1 and 100 sources")
    return value


def _unique_identities(identities: Sequence[tuple[str, str]]) -> None:
    ids = [identity[0] for identity in identities]
    shows = [identity[1] for identity in identities]
    if len(set(ids)) != len(ids) or len(set(shows)) != len(shows):
        raise PortableSourceError("Duplicate source logical ID or Spotify show ID")


def _parse_rows(value: object) -> tuple[PortableSource, ...]:
    result = tuple(_parse_source(row) for row in _source_rows(value))
    _unique_identities(
        [(item.source.id, item.source.external_references[0].external_id) for item in result]
    )
    return result


def _source_metadata(data: Mapping[str, object]) -> _SourceMetadata:
    original_id = _text(data["original_id"], 128)
    show_id = _text(data["spotify_show_id"], 22)
    if _ID.fullmatch(original_id) is None or not is_spotify_id(show_id):
        raise PortableSourceError("Invalid source logical ID or canonical Spotify show ID")
    name = _text(data["display_name"], 120)
    provenance = _text(data["provenance"], 16)
    if provenance not in {"built_in", "custom"}:
        raise PortableSourceError("Invalid source provenance")
    if type(data["enabled"]) is not bool:
        raise PortableSourceError("Source enabled must be a boolean")
    origin_data = _object(data["origin"], {"kind", "code"})
    try:
        origin = SourceOrigin(
            SourceOriginKind(_text(origin_data["kind"], 16)), _text(origin_data["code"], 12)
        )
        scope = EditorialScope(_text(data["editorial_scope"], 3))
        timezone = ZoneInfo(_text(data["timezone"], 128))
    except ValueError, ZoneInfoNotFoundError:
        raise PortableSourceError("Invalid source origin, scope or IANA timezone") from None
    countries = tuple(CountryCode(v) for v in _tags(data["countries"], _COUNTRY, minimum=0))
    languages = tuple(LanguageTag(v) for v in _tags(data["languages"], _LANGUAGE, minimum=1))
    return _SourceMetadata(
        SourceId(original_id),
        show_id,
        name,
        provenance,
        origin,
        scope,
        timezone,
        countries,
        languages,
        data["enabled"],
    )


def _parse_source(value: object) -> PortableSource:
    data = _object(value, _SOURCE_KEYS)
    metadata = _source_metadata(data)
    processing = _object(data["processing"], _PROCESSING_KEYS)
    parser = _capability(processing["parser"], _PARSER_V1)
    temporal = _capability(processing["temporal_strategy"], _TEMPORAL_V1)
    public = _capability(processing["public_release_mode"], _PUBLIC_V1)
    filter_id = (
        None
        if processing["collection_filter"] is None
        else _capability(processing["collection_filter"], _FILTER_V1)
    )
    if not has_title_parser(parser) or (
        filter_id is not None and not has_collection_filter(filter_id)
    ):
        raise UnsupportedSourceCapability("Source processing capability is not installed")
    return PortableSource(
        SourceDefinition(
            id=metadata.id,
            display_name=metadata.display_name,
            origin=metadata.origin,
            countries=metadata.countries,
            languages=metadata.languages,
            timezone=metadata.timezone,
            enabled=metadata.enabled,
            parser_id=ParserId(parser),
            temporal_strategy=TemporalStrategy(temporal),
            public_release_mode=PublicReleaseMode(public),
            collection_filter_id=filter_id,
            editorial_scope=metadata.editorial_scope,
            external_references=(ExternalReference("spotify", "show", metadata.spotify_show_id),),
        ),
        metadata.provenance,
    )


def _capability_shape(value: object) -> tuple[str, int, dict[str, object]]:
    data = _object(value, {"id", "version", "parameters"})
    identifier = _text(data["id"], 128)
    if type(data["version"]) is not int or data["version"] < 1:
        raise PortableSourceError("Invalid capability version")
    if not isinstance(data["parameters"], dict):
        raise PortableSourceError("Capability parameters must be an object")
    return identifier, data["version"], data["parameters"]


def _capability(value: object, supported: frozenset[str]) -> str:
    identifier, version, parameters = _capability_shape(value)
    if identifier not in supported or version != 1 or parameters:
        raise UnsupportedSourceCapability("Unsupported source processing capability or parameters")
    return identifier


def _object(value: object, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(value, dict) or value.keys() != keys:
        raise PortableSourceError("Invalid source document fields")
    return cast(Mapping[str, object], value)


def _text(value: object, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or not value.isprintable()
    ):
        raise PortableSourceError("Invalid source text field")
    return value


def _tags(value: object, pattern: re.Pattern[str], *, minimum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= 10:
        raise PortableSourceError("Invalid source classification list")
    tags = tuple(_text(item, 16) for item in value)
    if any(pattern.fullmatch(tag) is None for tag in tags) or len(set(tags)) != len(tags):
        raise PortableSourceError("Invalid or duplicate source classification tag")
    return tags


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PortableSourceError("Duplicate JSON field")
        result[key] = value
    return result


def _bad_constant(value: str) -> object:
    raise PortableSourceError("Non-finite JSON number")
