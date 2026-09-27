from __future__ import annotations

import re

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.base import RecoveringTitleParserBase
from news_bulletin_playlist.providers.common import (
    MADRID,
    parse_24h_clock_value,
    parse_numeric_date_value,
    unique_aware_datetime,
)

_PATTERN = re.compile(
    r"^(?:NOTICIAS\s+RNE|RNE\s+NOTICIAS)\s*(?:-\s*)?"
    r"(?P<date>[0-9]{1,2}\.[0-9]{1,2}\.(?:[0-9]{4}|[0-9]{2})\.?|[0-9]{6}|[0-9]{8})\s*-\s*"
    r"(?P<time>[0-9]{1,2}(?:[\.,][0-9]{2})?|[0-9]{3,4})\s*H?$",
    re.IGNORECASE,
)


class RneParser(RecoveringTitleParserBase):
    provider_id = "rne"

    def _parse_strict(self, title: str) -> ParsedEdition | None:
        match = _PATTERN.match(title.strip())
        if match is None:
            return None
        parsed_date = parse_numeric_date_value(
            match.group("date").removesuffix("."),
            order="dmy",
            separators=".",
            year_digits=(2, 4),
            compact=True,
            two_digit_year_policy="2000",
        )
        parsed_time = parse_24h_clock_value(
            match.group("time"),
            separators=".,",
            minute_optional=True,
            compact=True,
        )
        if parsed_date is None or parsed_time is None:
            return None
        hour, minute = parsed_time
        edition_at = unique_aware_datetime(
            parsed_date.year,
            parsed_date.month,
            parsed_date.day,
            hour,
            minute,
            tz=MADRID,
        )
        if edition_at is None:
            return None
        return ParsedEdition(self.provider_id, title, edition_at)
