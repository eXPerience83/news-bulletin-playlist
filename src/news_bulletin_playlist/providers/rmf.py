from __future__ import annotations

import re
from datetime import UTC, date, datetime

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.base import CatalogueParseContext
from news_bulletin_playlist.providers.common import clock_24h_pattern

RMF_FAKTY_PARSER_ID = "rmf_fakty"

_TIME_PREFIX = re.compile(
    rf"^{clock_24h_pattern(separators=':.')} +Fakty(?: |$)",
    re.IGNORECASE | re.ASCII,
)


class RmfFaktyParser:
    """Parse RMF Fakty catalogue titles using Spotify's day-level release date."""

    provider_id = RMF_FAKTY_PARSER_ID

    def parse(self, title: str) -> ParsedEdition | None:
        """Fail soft when trusted catalogue context is unavailable."""
        del title
        return None

    def parse_catalogue(
        self,
        title: str,
        *,
        context: CatalogueParseContext,
    ) -> ParsedEdition | None:
        """Combine the RMF local clock prefix with an exact Spotify release day."""
        if context.release_date_precision != "day" or context.timezone is None:
            return None

        match = _TIME_PREFIX.match(title.strip())
        if match is None:
            return None

        try:
            release_day = date.fromisoformat(context.release_date)
        except ValueError:
            return None
        if release_day.isoformat() != context.release_date:
            return None

        local_edition = datetime(
            release_day.year,
            release_day.month,
            release_day.day,
            int(match.group("hour")),
            int(match.group("minute")),
        )
        # A title and a calendar day cannot disambiguate a repeated DST hour, and
        # attaching tzinfo alone would also accept a nonexistent local hour.
        instants: set[datetime] = set()
        for fold in (0, 1):
            candidate_utc = local_edition.replace(tzinfo=context.timezone, fold=fold).astimezone(
                UTC
            )
            if candidate_utc.astimezone(context.timezone).replace(tzinfo=None) == local_edition:
                instants.add(candidate_utc)
        if len(instants) != 1:
            return None
        return ParsedEdition(self.provider_id, title, next(iter(instants)))
