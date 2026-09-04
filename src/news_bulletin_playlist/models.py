from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from enum import StrEnum
from typing import NewType
from zoneinfo import ZoneInfo

SourceId = NewType("SourceId", str)
PlaylistId = NewType("PlaylistId", str)
ParserId = NewType("ParserId", str)
AdapterId = NewType("AdapterId", str)
CountryCode = NewType("CountryCode", str)
LanguageTag = NewType("LanguageTag", str)

DEFAULT_DURATION_MAX_SECONDS = 1800


class OrderingPolicy(StrEnum):
    EDITION_AT_DESC = "edition_at_desc"
    PUBLISHED_AT_DESC = "published_at_desc"


class EditorialScope(StrEnum):
    """Primary editorial/geographic coverage of one source product."""

    LOCAL = "LOC"
    REGIONAL = "REG"
    NATIONAL = "NAT"
    INTERNATIONAL = "INT"
    MIXED = "MIX"


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
    editorial_scope: EditorialScope = EditorialScope.MIXED
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
class DurationPolicyException:
    """One narrow, bounded recurring exception to the default duration ceiling."""

    id: str
    source_id: SourceId
    edition_local_time: time
    max_seconds: int

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("duration exception id must not be empty")
        if self.edition_local_time.tzinfo is not None:
            raise ValueError("duration exception edition_local_time must be timezone-naive")
        if self.max_seconds <= 0:
            raise ValueError("duration exception max_seconds must be positive")


@dataclass(frozen=True, slots=True)
class DurationPolicy:
    """Common destination-side bulletin duration eligibility contract."""

    default_max_seconds: int = DEFAULT_DURATION_MAX_SECONDS
    exceptions: tuple[DurationPolicyException, ...] = ()

    def __post_init__(self) -> None:
        if self.default_max_seconds <= 0:
            raise ValueError("duration policy default_max_seconds must be positive")
        ids = [exception.id for exception in self.exceptions]
        if len(ids) != len(set(ids)):
            raise ValueError("duration policy exception ids must be unique")
        selectors = [
            (exception.source_id, exception.edition_local_time) for exception in self.exceptions
        ]
        if len(selectors) != len(set(selectors)):
            raise ValueError("duration policy exception selectors must be unique")
        for exception in self.exceptions:
            if exception.max_seconds <= self.default_max_seconds:
                raise ValueError("duration exception max_seconds must exceed default_max_seconds")


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
    duration_policy: DurationPolicy = field(default_factory=DurationPolicy)


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
