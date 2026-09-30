from __future__ import annotations

import re

from news_bulletin_playlist.providers.common import (
    MADRID,
    SPANISH_MONTH_NAMES,
    clock_24h_pattern,
    named_month_date_pattern,
    spanish_month,
)
from news_bulletin_playlist.providers.temporal_profile import ProfileTitleParser, TemporalProfile

_CLOCK = clock_24h_pattern(separators=":")
_DATE = named_month_date_pattern(
    order="dmy",
    month_names=SPANISH_MONTH_NAMES,
    year_digits=(4,),
)
_PATTERN = re.compile(
    rf"^{_CLOCK}H\s*\|\s*{_DATE}\s*\|\s*(?:BOLETÍN|BOLETIN)$",
    re.IGNORECASE,
)


class CopeParser(ProfileTitleParser):
    provider_id = "cope"
    default_timezone = MADRID
    profile = TemporalProfile(_PATTERN, month_resolver=spanish_month)
