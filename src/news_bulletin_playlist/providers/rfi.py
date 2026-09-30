from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from news_bulletin_playlist.providers.common import (
    clock_24h_pattern,
    numeric_date_pattern,
)
from news_bulletin_playlist.providers.temporal_profile import ProfileTitleParser, TemporalProfile

UTC = ZoneInfo("UTC")

_DATE = numeric_date_pattern(order="dmy", separators="/", year_digits=(4,))
_CLOCK = clock_24h_pattern(separators="h")
_PATTERN = re.compile(
    rf"^Journal\s+{_DATE}\s+{_CLOCK}\s+GMT$",
    re.IGNORECASE,
)


class RfiJournalMondeParser(ProfileTitleParser):
    provider_id = "rfi_journal_monde"
    default_timezone = UTC
    profile = TemporalProfile(_PATTERN)
