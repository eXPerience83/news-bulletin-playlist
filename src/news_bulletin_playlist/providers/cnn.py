from __future__ import annotations

import re

from news_bulletin_playlist.providers.common import (
    NEW_YORK,
    clock_12h_pattern,
    numeric_date_pattern,
)
from news_bulletin_playlist.providers.temporal_profile import ProfileTitleParser, TemporalProfile

_DATE = numeric_date_pattern(order="mdy", separators="/", year_digits=(2, 4))
_CLOCK = clock_12h_pattern(separators=":", minute_optional=True)
_PATTERN = re.compile(
    rf"^CNN 5 cosas\s+{_DATE}\s+{_CLOCK}\s*(?P<ampm>am|pm)$",
    re.IGNORECASE,
)


class CnnParser(ProfileTitleParser):
    provider_id = "cnn"
    default_timezone = NEW_YORK
    profile = TemporalProfile(_PATTERN, clock="12h")
