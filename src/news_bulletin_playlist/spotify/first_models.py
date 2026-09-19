"""Spotify-first domain models and time helpers for Phase C shadow mode."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.spotify.uris import is_spotify_episode_uri

_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_PRECISIONS = frozenset({"day", "month", "year"})


class TimestampSource(StrEnum):
    TITLE = "title"
    FIRST_SEEN = "first_seen"


class RefreshReason(StrEnum):
    BOOTSTRAP = "bootstrap"
    CHANGE = "change"
    INACTIVITY_AUDIT = "inactivity_audit"


@dataclass(frozen=True, slots=True)
class SpotifyFirstEpisode:
    source_id: SourceId
    spotify_show_id: str
    spotify_episode_id: str
    spotify_episode_uri: str
    title: str
    edition_at: datetime | None
    timestamp_source: TimestampSource
    release_date_raw: str
    release_date_precision: str
    duration_seconds: int | None
    first_seen_at: datetime
    last_seen_at: datetime

    def __post_init__(self) -> None:
        spotify_id(self.spotify_show_id)
        spotify_id(self.spotify_episode_id)
        if not is_spotify_episode_uri(self.spotify_episode_uri):
            raise ValueError("invalid Spotify episode URI")
        if self.spotify_episode_uri != f"spotify:episode:{self.spotify_episode_id}":
            raise ValueError("Spotify episode URI/id mismatch")
        if not self.title.strip():
            raise ValueError("Spotify episode title must not be blank")
        release_period(self.release_date_raw, self.release_date_precision)
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")
        first, last = as_utc(self.first_seen_at), as_utc(self.last_seen_at)
        if last < first:
            raise ValueError("last_seen_at precedes first_seen_at")
        object.__setattr__(self, "first_seen_at", first)
        object.__setattr__(self, "last_seen_at", last)
        if self.edition_at is not None:
            object.__setattr__(self, "edition_at", as_utc(self.edition_at))
        if (self.timestamp_source is TimestampSource.TITLE) != (self.edition_at is not None):
            raise ValueError("timestamp provenance disagrees with edition_at")


@dataclass(frozen=True, slots=True)
class SourceShadowState:
    source_id: SourceId
    spotify_show_id: str
    last_refresh_at: datetime
    reason: RefreshReason
    detector_episode_id: str | None
    api_episode_id: str | None
    request_count: int
    catalogue_complete: bool
    coverage_proven: bool
    coverage_cutoff_at: datetime


@dataclass(frozen=True, slots=True)
class UpsertCounts:
    added: int
    updated: int
    unchanged: int
    removed: int = 0


def release_period(value: str, precision: str) -> tuple[date, date]:
    if precision not in _PRECISIONS:
        raise ValueError("invalid Spotify release precision")
    if precision == "day":
        day = date.fromisoformat(value)
        return day, day
    if precision == "month":
        if len(value) != 7:
            raise ValueError("invalid month release date")
        year, month = (int(part) for part in value.split("-"))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    if len(value) != 4:
        raise ValueError("invalid year release date")
    year = int(value)
    return date(year, 1, 1), date(year, 12, 31)


def episode_order_key(episode: SpotifyFirstEpisode) -> tuple[int, int, int, str, str]:
    if episode.edition_at is not None:
        value = episode.edition_at
        day = value.date()
    else:
        day, _ = release_period(episode.release_date_raw, episode.release_date_precision)
        value = episode.first_seen_at
    return (
        day.toordinal(),
        value.hour * 3600 + value.minute * 60 + value.second,
        value.microsecond,
        str(episode.source_id),
        episode.spotify_episode_id,
    )


def spotify_id(value: str) -> str:
    if _ID.fullmatch(value) is None:
        raise ValueError("invalid Spotify id")
    return value


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
