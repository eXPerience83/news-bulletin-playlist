from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
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


class EditorialScope(StrEnum):
    """Primary editorial/geographic coverage of one source product."""

    LOCAL = "LOC"
    REGIONAL = "REG"
    NATIONAL = "NAT"
    INTERNATIONAL = "INT"
    MIXED = "MIX"


class SourceOriginKind(StrEnum):
    COUNTRY = "country"
    INSTITUTION = "institution"


@dataclass(frozen=True, slots=True)
class SourceOrigin:
    """Editorial provider origin, independent of operational country metadata."""

    kind: SourceOriginKind
    code: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceOriginKind):
            raise ValueError("source origin kind must be country or institution")
        if (
            not isinstance(self.code, str)
            or not self.code.isascii()
            or not self.code.isalpha()
            or not self.code.isupper()
        ):
            raise ValueError("source origin code must be uppercase ASCII letters")
        if self.kind is SourceOriginKind.COUNTRY and len(self.code) != 2:
            raise ValueError("country origin must be a two-letter code")
        if self.kind is SourceOriginKind.COUNTRY and self.code == "UN":
            raise ValueError("UN is an institutional origin, not a country")
        if self.kind is SourceOriginKind.INSTITUTION and not 2 <= len(self.code) <= 12:
            raise ValueError("institution origin must be 2 to 12 letters")

    @classmethod
    def country(cls, code: CountryCode) -> SourceOrigin:
        return cls(SourceOriginKind.COUNTRY, str(code))

    @classmethod
    def institution(cls, code: str) -> SourceOrigin:
        return cls(SourceOriginKind.INSTITUTION, code)

    def __str__(self) -> str:
        return self.code


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
    collection_filter_id: str | None = None
    editorial_scope: EditorialScope = EditorialScope.MIXED
    external_references: tuple[ExternalReference, ...] = ()
    origin: SourceOrigin = field(kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.origin, SourceOrigin):
            raise ValueError("source origin must be a SourceOrigin")
        if self.collection_filter_id is not None and not self.collection_filter_id.strip():
            raise ValueError("collection_filter_id must not be blank")


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
