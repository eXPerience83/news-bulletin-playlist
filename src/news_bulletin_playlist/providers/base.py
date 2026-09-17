from __future__ import annotations

from typing import Protocol

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.title_recovery import TitleParseAttempt


class TitleParser(Protocol):
    provider_id: str

    def parse(self, title: str) -> ParsedEdition | None: ...


class RecoveryTitleParser(TitleParser, Protocol):
    """Title parser that exposes the exact/recovery decision used by runtime and watch."""

    def parse_with_recovery(self, title: str) -> TitleParseAttempt: ...
