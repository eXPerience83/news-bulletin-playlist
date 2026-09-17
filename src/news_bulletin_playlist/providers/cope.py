from __future__ import annotations

import re

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.common import MADRID, aware_datetime, spanish_month
from news_bulletin_playlist.providers.title_recovery import TitleParseAttempt, parse_with_recovery

_PATTERN = re.compile(
    r"^(?P<hour>[01]?[0-9]|2[0-3]):(?P<minute>[0-5][0-9])H\s*\|\s*"
    r"(?P<day>[0-9]{1,2})\s+(?P<month>[A-ZÁÉÍÓÚÜÑ]{3,5})\s+(?P<year>[0-9]{4})\s*\|\s*"
    r"(?P<label>.+)$",
    re.IGNORECASE,
)


def _is_national_bulletin(label: str) -> bool:
    normalized = re.sub(r"\s+", " ", label.strip().upper())
    return normalized in {"BOLETÍN", "BOLETIN"}


class CopeParser:
    provider_id = "cope"

    def parse(self, title: str) -> ParsedEdition | None:
        return self.parse_with_recovery(title).parsed

    def parse_with_recovery(self, title: str) -> TitleParseAttempt:
        return parse_with_recovery(self.provider_id, title, self._parse_strict)

    def _parse_strict(self, title: str) -> ParsedEdition | None:
        match = _PATTERN.match(title.strip())
        if match is None or not _is_national_bulletin(match.group("label")):
            return None
        month = spanish_month(match.group("month"))
        if month is None:
            return None
        try:
            edition_at = aware_datetime(
                int(match.group("year")),
                month,
                int(match.group("day")),
                int(match.group("hour")),
                int(match.group("minute")),
                tz=MADRID,
            )
        except ValueError:
            return None
        return ParsedEdition(self.provider_id, title, edition_at)
