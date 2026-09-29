"""Reviewed title layouts; temporal semantics live in the common primitives."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.common import (
    ClockFormat,
    MonthResolver,
    datetime_from_match,
    parse_24h_clock_value,
    parse_numeric_date_value,
    unique_aware_datetime,
)
from news_bulletin_playlist.providers.title_recovery import TitleParseAttempt, parse_with_recovery


@dataclass(frozen=True, slots=True)
class TemporalProfile:
    pattern: re.Pattern[str]
    clock: ClockFormat = "24h"
    month_resolver: MonthResolver | None = None
    compact_fields: bool = False

    def resolve(self, title: str, timezone: ZoneInfo) -> datetime | None:
        match = self.pattern.fullmatch(title.strip())
        if match is None:
            return None
        if not self.compact_fields:
            return datetime_from_match(
                match, tz=timezone, clock=self.clock, month_resolver=self.month_resolver
            )
        day = parse_numeric_date_value(
            match.group("date").removesuffix("."),
            order="dmy",
            separators=".",
            year_digits=(2, 4),
            compact=True,
            two_digit_year_policy="2000",
        )
        clock = parse_24h_clock_value(
            match.group("time"), separators=".,", minute_optional=True, compact=True
        )
        if day is None or clock is None:
            return None
        return unique_aware_datetime(day.year, day.month, day.day, *clock, tz=timezone)


class ProfileTitleParser:
    """Compatibility API and recovery policy around a declarative temporal layout.

    Runtime supplies the source timezone; the default is only for context-free callers.
    """

    provider_id: str
    profile: TemporalProfile
    default_timezone: ZoneInfo

    def parse(self, title: str) -> ParsedEdition | None:
        return self.parse_with_recovery(title).parsed

    def parse_with_recovery(self, title: str) -> TitleParseAttempt:
        return self.parse_in_timezone(title, self.default_timezone)

    def parse_in_timezone(self, title: str, timezone: ZoneInfo) -> TitleParseAttempt:
        def resolve(candidate: str) -> ParsedEdition | None:
            value = self.profile.resolve(candidate, timezone)
            return None if value is None else ParsedEdition(self.provider_id, candidate, value)

        return parse_with_recovery(self.provider_id, title, resolve)


@dataclass(frozen=True, slots=True)
class CatalogueClockProfile:
    """Reviewed edition-clock prefix with a catalogue-supplied calendar day."""

    pattern: re.Pattern[str]

    def resolve(self, title: str, day: date, timezone: ZoneInfo) -> datetime | None:
        match = self.pattern.match(title.strip())
        if match is None:
            return None
        # A second numeric field may be a malformed clock/date that token discovery
        # cannot recognize. A reviewed prefix must not hide that competing evidence.
        if any(character.isnumeric() for character in title.strip()[match.end() :]):
            return None
        return unique_aware_datetime(
            day.year,
            day.month,
            day.day,
            int(match.group("hour")),
            int(match.group("minute")),
            tz=timezone,
        )
