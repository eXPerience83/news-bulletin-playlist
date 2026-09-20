from __future__ import annotations

import re

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.base import RecoveringTitleParserBase
from news_bulletin_playlist.providers.common import MADRID, aware_datetime

_PATTERN = re.compile(
    r"^Las noticias de Onda Cero de las\s+"
    r"(?P<hour>[01]?[0-9]|2[0-3]):(?P<minute>[0-5][0-9])h\s*"
    r"\((?P<day>[0-9]{1,2})/(?P<month>[0-9]{1,2})/(?P<year>[0-9]{4})\)$",
    re.IGNORECASE,
)


class OndaCeroParser(RecoveringTitleParserBase):
    provider_id = "ondacero"

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
