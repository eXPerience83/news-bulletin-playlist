from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.common import aware_datetime

UTC = ZoneInfo("UTC")

_PATTERN = re.compile(
    r"^Journal\s+"
    r"(?P<day>\d{1,2})/(?P<month>\d{1,2})/(?P<year>\d{4})\s+"
    r"(?P<hour>[01]?\d|2[0-3])h(?P<minute>[0-5]\d)\s+GMT$",
    re.IGNORECASE,
)


class RfiJournalMondeParser:
    """Accept only RFI Journal Monde bulletin titles with an explicit GMT edition time."""

    provider_id = "rfi_journal_monde"

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
                tz=UTC,
            )
        except ValueError:
            return None
        return ParsedEdition(self.provider_id, title, edition_at)
