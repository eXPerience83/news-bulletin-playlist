from __future__ import annotations

import re
from datetime import UTC, date

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.base import CatalogueParseContext
from news_bulletin_playlist.providers.common import clock_24h_pattern
from news_bulletin_playlist.providers.temporal_profile import CatalogueClockProfile

RMF_FAKTY_PARSER_ID = "rmf_fakty"

_TIME_PREFIX = re.compile(
    rf"^{clock_24h_pattern(separators=':.')} +Fakty(?: |$)",
    re.IGNORECASE | re.ASCII,
)


class RmfFaktyParser:
    """Parse RMF Fakty catalogue titles using Spotify's day-level release date."""

    provider_id = RMF_FAKTY_PARSER_ID
    profile = CatalogueClockProfile(_TIME_PREFIX)

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

        try:
            release_day = date.fromisoformat(context.release_date)
        except ValueError:
            return None
        if release_day.isoformat() != context.release_date:
            return None

        local_edition = self.profile.resolve(title, release_day, context.timezone)
        if local_edition is None:
            return None
        return ParsedEdition(self.provider_id, title, local_edition.astimezone(UTC))
