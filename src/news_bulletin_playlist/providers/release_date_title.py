from __future__ import annotations

from news_bulletin_playlist.models import ParsedEdition

RELEASE_DATE_TITLE_PARSER_ID = "release_date_title"


class ReleaseDateTitleParser:
    """Parser marker for products without a trustworthy semantic title timestamp.

    The Spotify-first catalogue keeps ``edition_at`` unset for these titles. Episode identity
    remains the Spotify episode ID/URI and ordering/retention use explicit release-date precision
    plus local first-seen provenance rather than an RSS-to-Spotify match.
    """

    provider_id = RELEASE_DATE_TITLE_PARSER_ID

    def parse(self, title: str) -> ParsedEdition | None:
        del title
        return None
