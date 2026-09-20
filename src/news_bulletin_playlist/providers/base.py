from __future__ import annotations

from typing import Protocol

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.title_recovery import TitleParseAttempt, parse_with_recovery


class TitleParser(Protocol):
    provider_id: str

    def parse(self, title: str) -> ParsedEdition | None: ...


class RecoveryTitleParser(TitleParser, Protocol):
    """Title parser that exposes the exact/recovery decision behind semantic parsing."""

    def parse_with_recovery(self, title: str) -> TitleParseAttempt: ...


class RecoveringTitleParserBase:
    """Shared recovery plumbing; concrete parsers only implement strict provider grammar."""

    provider_id: str

    def parse(self, title: str) -> ParsedEdition | None:
        return self.parse_with_recovery(title).parsed

    def parse_with_recovery(self, title: str) -> TitleParseAttempt:
        return parse_with_recovery(self.provider_id, title, self._parse_strict)

    def _parse_strict(self, title: str) -> ParsedEdition | None:
        raise NotImplementedError
