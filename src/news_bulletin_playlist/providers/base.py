from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.title_recovery import TitleParseAttempt, parse_with_recovery


@dataclass(frozen=True, slots=True)
class CatalogueParseContext:
    """Trusted catalogue metadata that a parser may combine with title evidence."""

    release_date: str
    release_date_precision: str
    timezone: ZoneInfo | None


class TitleParser(Protocol):
    provider_id: str

    def parse(self, title: str) -> ParsedEdition | None: ...


@runtime_checkable
class RecoveryTitleParser(TitleParser, Protocol):
    """Title parser that exposes the exact/recovery decision behind semantic parsing."""

    def parse_with_recovery(self, title: str) -> TitleParseAttempt: ...


@runtime_checkable
class CatalogueTitleParser(TitleParser, Protocol):
    """Parser that can combine title evidence with trusted Spotify catalogue metadata."""

    def parse_catalogue(
        self,
        title: str,
        *,
        context: CatalogueParseContext,
    ) -> ParsedEdition | None: ...


class RecoveringTitleParserBase:
    """Shared recovery plumbing; concrete parsers only implement strict provider grammar."""

    provider_id: str

    def parse(self, title: str) -> ParsedEdition | None:
        return self.parse_with_recovery(title).parsed

    def parse_with_recovery(self, title: str) -> TitleParseAttempt:
        return parse_with_recovery(self.provider_id, title, self._parse_strict)

    def _parse_strict(self, title: str) -> ParsedEdition | None:
        raise NotImplementedError


def parse_title(
    parser: TitleParser,
    title: str,
    *,
    context: CatalogueParseContext | None = None,
) -> TitleParseAttempt:
    """Run one parser through every supported evidence path, strongest first."""

    if context is not None and isinstance(parser, CatalogueTitleParser):
        parsed = parser.parse_catalogue(title, context=context)
        if parsed is not None:
            return TitleParseAttempt(parsed, "catalogue_context")
    if isinstance(parser, RecoveryTitleParser):
        return parser.parse_with_recovery(title)
    parsed = parser.parse(title)
    return TitleParseAttempt(parsed, "exact" if parsed is not None else "rejected")
