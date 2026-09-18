from __future__ import annotations

import re

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.common import MADRID, aware_datetime
from news_bulletin_playlist.providers.title_recovery import TitleParseAttempt, parse_with_recovery

_PATTERN = re.compile(
    r"^Las noticias de la SER,?\s+"
    r"(?P<hour>[01]?[0-9]|2[0-3]):(?P<minute>[0-5][0-9])\s*"
    r"\((?P<day>[0-9]{1,2})/(?P<month>[0-9]{1,2})/(?P<year>[0-9]{4})\)$",
    re.IGNORECASE,
)


class SerParser:
    provider_id = "ser"

    def parse(self, title: str) -> ParsedEdition | None:
        return self.parse_with_recovery(title).parsed

    def parse_with_recovery(self, title: str) -> TitleParseAttempt:
        return parse_with_recovery(self.provider_id, title, self._parse_strict)

    def _parse_strict(self, title: str) -> ParsedEdition | None:
        match = _PATTERN.match(title.strip())
        if match is None:
            return None
        values = {key: int(value) for key, value in match.groupdict().items()}
        try:
            edition_at = aware_datetime(
                values["year"],
                values["month"],
                values["day"],
                values["hour"],
                values["minute"],
                tz=MADRID,
            )
        except ValueError:
            return None
        return ParsedEdition(self.provider_id, title, edition_at)
