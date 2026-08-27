from __future__ import annotations

import re

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.common import NEW_YORK, aware_datetime, expand_two_digit_year

_PATTERN = re.compile(
    r"^CNN 5 cosas\s+"
    r"(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{2}|\d{4})\s+"
    r"(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*(?P<ampm>am|pm)$",
    re.IGNORECASE,
)


class CnnParser:
    provider_id = "cnn"

    def parse(self, title: str) -> ParsedEdition | None:
        match = _PATTERN.match(title.strip())
        if match is None:
            return None
        hour = int(match.group("hour"))
        if not 1 <= hour <= 12:
            return None
        minute = int(match.group("minute") or "0")
        if match.group("ampm").lower() == "pm" and hour != 12:
            hour += 12
        elif match.group("ampm").lower() == "am" and hour == 12:
            hour = 0
        try:
            edition_at = aware_datetime(
                expand_two_digit_year(int(match.group("year"))),
                int(match.group("month")),
                int(match.group("day")),
                hour,
                minute,
                tz=NEW_YORK,
            )
        except ValueError:
            return None
        return ParsedEdition(self.provider_id, title, edition_at)
