from __future__ import annotations

from typing import Protocol

from news_bulletin_playlist.models import ParsedEdition


class TitleParser(Protocol):
    provider_id: str

    def parse(self, title: str) -> ParsedEdition | None: ...
