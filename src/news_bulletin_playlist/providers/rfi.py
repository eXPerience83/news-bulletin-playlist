from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.base import RecoveringTitleParserBase
from news_bulletin_playlist.providers.common import (
    clock_24h_pattern,
    datetime_from_match,
    numeric_date_pattern,
)

UTC = ZoneInfo("UTC")

_DATE = numeric_date_pattern(order="dmy", separators="/", year_digits=(4,))
_CLOCK = clock_24h_pattern(separators="h")
_PATTERN = re.compile(
    rf"^Journal\s+{_DATE}\s+{_CLOCK}\s+GMT$",
    re.IGNORECASE,
)


class RfiJournalMondeParser(RecoveringTitleParserBase):
    """Accept only RFI Journal Monde bulletin titles with an explicit GMT edition time."""

    provider_id = "rfi_journal_monde"

    def _parse_strict(self, title: str) -> ParsedEdition | None:
        match = _PATTERN.match(title.strip())
        if match is None:
            return None
        edition_at = datetime_from_match(match, tz=UTC)
        if edition_at is None:
            return None
        return ParsedEdition(self.provider_id, title, edition_at)
