from __future__ import annotations

import re

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.base import RecoveringTitleParserBase
from news_bulletin_playlist.providers.common import (
    NEW_YORK,
    clock_12h_pattern,
    datetime_from_match,
    numeric_date_pattern,
)

_DATE = numeric_date_pattern(order="mdy", separators="/", year_digits=(2, 4))
_CLOCK = clock_12h_pattern(separators=":", minute_optional=True)
_PATTERN = re.compile(
    rf"^CNN 5 cosas\s+{_DATE}\s+{_CLOCK}\s*(?P<ampm>am|pm)$",
    re.IGNORECASE,
)


class CnnParser(RecoveringTitleParserBase):
    provider_id = "cnn"

    def _parse_strict(self, title: str) -> ParsedEdition | None:
        match = _PATTERN.match(title.strip())
        if match is None:
            return None
        edition_at = datetime_from_match(match, tz=NEW_YORK, clock="12h")
        if edition_at is None:
            return None
        return ParsedEdition(self.provider_id, title, edition_at)
