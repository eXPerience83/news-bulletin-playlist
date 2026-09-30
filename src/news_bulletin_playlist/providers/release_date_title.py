from __future__ import annotations

from news_bulletin_playlist.models import ParsedEdition

RELEASE_DATE_TITLE_PARSER_ID = "release_date_title"


class ReleaseDateTitleParser:
    """Compatibility marker for sources without a historical reviewed parser.

    Universal runtime detection may now prove semantic evidence for these sources.
    Context-free callers still fail soft; catalogue precision and first-seen remain
    the fallback whenever runtime cannot prove an exact semantic timestamp.
    """

    provider_id = RELEASE_DATE_TITLE_PARSER_ID

    def parse(self, title: str) -> ParsedEdition | None:
        del title
        return None
