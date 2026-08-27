from __future__ import annotations

import re

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.common import MADRID, aware_datetime

_PATTERN = re.compile(
    r"^Las noticias de la SER,?\s+"
    r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\s*"
    r"\((?P<day>\d{1,2})/(?P<month>\d{1,2})/(?P<year>\d{4})\)$",
    re.IGNORECASE,
)


class SerParser:
    provider_id = "ser"

    def parse(self, title: str) -> ParsedEdition | None:
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
