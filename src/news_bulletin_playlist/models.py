from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import NewType
from zoneinfo import ZoneInfo

SourceId = NewType("SourceId", str)
PlaylistId = NewType("PlaylistId", str)
ParserId = NewType("ParserId", str)
AdapterId = NewType("AdapterId", str)
CountryCode = NewType("CountryCode", str)
LanguageTag = NewType("LanguageTag", str)


class OrderingPolicy(StrEnum):
    EDITION_AT_DESC = "edition_at_desc"
    PUBLISHED_AT_DESC = "published_at_desc"


@dataclass(frozen=True, slots=True)
class ExternalReference:
    """A source's identity in an external catalogue, never a write destination."""

    system: str
    resource_type: str
    external_id: str


@dataclass(frozen=True, slots=True)
class DestinationReference:
    """A destination managed by an adapter, such as a Spotify playlist."""

    adapter_id: AdapterId
    external_id: str


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    id: SourceId
    display_name: str
    countries: tuple[CountryCode, ...]
    languages: tuple[LanguageTag, ...]
    timezone: ZoneInfo
    enabled: bool
    parser_id: ParserId
    endpoint_url: str | None = None
    external_references: tuple[ExternalReference, ...] = ()
    spotify_release_delay_days: int = 0

    def __post_init__(self) -> None:
        if self.spotify_release_delay_days < 0:
            raise ValueError("spotify_release_delay_days must be non-negative")


@dataclass(frozen=True, slots=True)
class CanonicalEdition:
    """Provider-independent metadata for one source-native bulletin asset."""

    source_id: SourceId
    source_native_id: str
    title: str
    published_at: datetime
    edition_at: datetime | None
    duration_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.source_native_id.strip():
            raise ValueError("source_native_id must not be empty")
        if not self.title.strip():
            raise ValueError("title must not be empty")
        object.__setattr__(self, "published_at", _as_utc(self.published_at, "published_at"))
        if self.edition_at is not None:
            object.__setattr__(self, "edition_at", _as_utc(self.edition_at, "edition_at"))
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")

    @property
    def identity(self) -> tuple[SourceId, str]:
        return (self.source_id, self.source_native_id)


@dataclass(frozen=True, slots=True)
class SourceSelection:
    """Authoritative explicit selection for schema v1."""

    explicit: tuple[SourceId, ...] = ()


@dataclass(frozen=True, slots=True)
class PlaylistDefinition:
    id: PlaylistId
    display_name: str
    description: str
    countries: tuple[CountryCode, ...]
    languages: tuple[LanguageTag, ...]
    enabled: bool
    source_selection: SourceSelection
    destination: DestinationReference
    retention_hours: int = 48
    max_episodes: int = 100
    ordering: OrderingPolicy = OrderingPolicy.EDITION_AT_DESC


@dataclass(frozen=True, slots=True)
class EngineConfig:
    schema_version: int
    sources: tuple[SourceDefinition, ...]
    playlists: tuple[PlaylistDefinition, ...]


@dataclass(frozen=True, slots=True)
class ParsedEdition:
    """Intermediate provider-title parse result; not a canonical edition."""

    provider_id: str
    title: str
    edition_at: datetime

    def __post_init__(self) -> None:
        if self.edition_at.tzinfo is None or self.edition_at.utcoffset() is None:
            raise ValueError("edition_at must be timezone-aware")


def _as_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)
