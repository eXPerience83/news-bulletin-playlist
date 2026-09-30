from __future__ import annotations

import re

from news_bulletin_playlist.providers.common import (
    MADRID,
)
from news_bulletin_playlist.providers.temporal_profile import ProfileTitleParser, TemporalProfile

_PATTERN = re.compile(
    r"^(?:NOTICIAS\s+RNE|RNE\s+NOTICIAS)\s*(?:-\s*)?"
    r"(?P<date>[0-9]{1,2}\.[0-9]{1,2}\.(?:[0-9]{4}|[0-9]{2})\.?|[0-9]{6}|[0-9]{8})\s*-\s*"
    r"(?P<time>[0-9]{1,2}(?:[\.,][0-9]{2})?|[0-9]{3,4})\s*H?$",
    re.IGNORECASE,
)


class RneParser(ProfileTitleParser):
    provider_id = "rne"
    default_timezone = MADRID
    profile = TemporalProfile(_PATTERN, compact_fields=True)
