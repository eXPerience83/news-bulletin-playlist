from __future__ import annotations

import re

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.base import RecoveringTitleParserBase
from news_bulletin_playlist.providers.common import (
    MADRID,
    clock_24h_pattern,
    datetime_from_match,
    numeric_date_pattern,
)

_CLOCK = clock_24h_pattern(separators=":")
_DATE = numeric_date_pattern(order="dmy", separators="/", year_digits=(4,))
_PATTERN = re.compile(
    rf"^Las noticias de la SER,?\s+{_CLOCK}\s*\({_DATE}\)$",
    re.IGNORECASE,
)


class SerParser(RecoveringTitleParserBase):
    provider_id = "ser"

    def _parse_strict(self, title: str) -> ParsedEdition | None:
        match = _PATTERN.match(title.strip())
        if match is None:
            return None
        edition_at = datetime_from_match(match, tz=MADRID)
        if edition_at is None:
            return None
        return ParsedEdition(self.provider_id, title, edition_at)
