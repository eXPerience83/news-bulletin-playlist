from __future__ import annotations

import re

from news_bulletin_playlist.providers.common import (
    MADRID,
    clock_24h_pattern,
    numeric_date_pattern,
)
from news_bulletin_playlist.providers.temporal_profile import ProfileTitleParser, TemporalProfile

_CLOCK = clock_24h_pattern(separators=":")
_DATE = numeric_date_pattern(order="dmy", separators="/", year_digits=(4,))
_PATTERN = re.compile(
    rf"^Las noticias de Onda Cero de las\s+{_CLOCK}h\s*\({_DATE}\)$",
    re.IGNORECASE,
)


class OndaCeroParser(ProfileTitleParser):
    provider_id = "ondacero"
    default_timezone = MADRID
    profile = TemporalProfile(_PATTERN)
