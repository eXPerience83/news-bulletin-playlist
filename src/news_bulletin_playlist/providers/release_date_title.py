from __future__ import annotations

from news_bulletin_playlist.models import ParsedEdition

RELEASE_DATE_TITLE_PARSER_ID = "release_date_title"


class ReleaseDateTitleParser:
    """Marker parser for feeds matched by exact title plus Spotify release date.

    These feeds do not encode a trustworthy semantic edition timestamp in the title.
    Collection therefore keeps ``edition_at`` unset and the Spotify matcher uses the
    already-supported exact normalized title + release-date path.
    """

    provider_id = RELEASE_DATE_TITLE_PARSER_ID

    def parse(self, title: str) -> ParsedEdition | None:
        del title
        return None
