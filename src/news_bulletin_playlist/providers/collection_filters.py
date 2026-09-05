"""Narrow, deterministic product filters applied before RSS normalization."""

from __future__ import annotations

import re
from datetime import date
from typing import Protocol


class CollectionFilter(Protocol):
    def accepts(self, title: str) -> bool: ...


class UnNewsEsMinutesFilter:
    """Accept only dated ``La ONU en Minutos`` bulletin editions.

    The United Nations Spanish audio RSS is a multi-product feed.  This filter
    deliberately admits only its dated ONU-en-minutos product, leaving the
    semantic edition time unset for the release-date/title matcher.
    """

    filter_id = "un_news_es_minutes"
    _pattern = re.compile(
        r"^La ONU en Minutos\s+(?P<day>0?[1-9]|[12]\d|3[01])"
        r"(?:\s+de)?\s+(?P<month>enero|febrero|marzo|abril|mayo|junio|julio|"
        r"agosto|septiembre|octubre|noviembre|diciembre)\s+de\s+(?P<year>\d{4})$",
        re.IGNORECASE,
    )
    _months = {
        "enero": 1,
        "febrero": 2,
        "marzo": 3,
        "abril": 4,
        "mayo": 5,
        "junio": 6,
        "julio": 7,
        "agosto": 8,
        "septiembre": 9,
        "octubre": 10,
        "noviembre": 11,
        "diciembre": 12,
    }

    def accepts(self, title: str) -> bool:
        match = self._pattern.fullmatch(title.strip())
        if match is None:
            return False
        try:
            date(
                int(match.group("year")),
                self._months[match.group("month").casefold()],
                int(match.group("day")),
            )
        except ValueError:
            return False
        return True
