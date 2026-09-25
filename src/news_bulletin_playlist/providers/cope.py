from __future__ import annotations

import re

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.base import RecoveringTitleParserBase
from news_bulletin_playlist.providers.common import (
    MADRID,
    SPANISH_MONTH_NAMES,
    clock_24h_pattern,
    datetime_from_match,
    named_month_date_pattern,
    spanish_month,
)

_CLOCK = clock_24h_pattern(separators=":")
_DATE = named_month_date_pattern(
    order="dmy",
    month_names=SPANISH_MONTH_NAMES,
    year_digits=(4,),
)
_PATTERN = re.compile(
    rf"^{_CLOCK}H\s*\|\s*{_DATE}\s*\|\s*(?P<label>.+)$",
    re.IGNORECASE,
)


def _is_national_bulletin(label: str) -> bool:
    normalized = re.sub(r"\s+", " ", label.strip().upper())
    return normalized in {"BOLETÍN", "BOLETIN"}


class CopeParser(RecoveringTitleParserBase):
    provider_id = "cope"

    def _parse_strict(self, title: str) -> ParsedEdition | None:
        match = _PATTERN.match(title.strip())
        if match is None or not _is_national_bulletin(match.group("label")):
            return None
        edition_at = datetime_from_match(
            match,
            tz=MADRID,
            month_resolver=spanish_month,
        )
        if edition_at is None:
            return None
        return ParsedEdition(self.provider_id, title, edition_at)
