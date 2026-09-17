from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from news_bulletin_playlist.models import ParsedEdition

type TitleParseMode = Literal["exact", "normalized", "provider_compat", "rejected"]
type StrictTitleParser = Callable[[str], ParsedEdition | None]

_DASHES = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)
_ZERO_WIDTH = str.maketrans({"\u200b": None, "\u200c": None, "\u200d": None, "\ufeff": None})

_RNE_COMPAT = re.compile(
    r"^(?P<product>NOTICIAS\s+RNE|RNE\s+NOTICIAS)\s*(?:[-|:]\s*)?"
    r"(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>\d{2}|\d{4})\.?\s*"
    r"(?:[-|:]\s*)?(?P<hour>\d{1,2})(?:[.:,](?P<minute>\d{2}))?\s*H?\.?$",
    re.IGNORECASE,
)
_SER_COMPAT = re.compile(
    r"^Las\s+noticias\s+de\s+la\s+SER,?\s*(?:[-|:]\s*)?"
    r"(?P<hour>\d{1,2})[.:,](?P<minute>\d{2})\s*"
    r"(?:\(|\[)?\s*(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>\d{2}|\d{4})"
    r"\s*(?:\)|\])?\.?$",
    re.IGNORECASE,
)
_ONDACERO_COMPAT = re.compile(
    r"^Las\s+noticias\s+de\s+Onda\s+Cero\s+de\s+las\s+"
    r"(?P<hour>\d{1,2})[.:,](?P<minute>\d{2})\s*[hH]?\s*"
    r"(?:\(|\[)?\s*(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>\d{2}|\d{4})"
    r"\s*(?:\)|\])?\.?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TitleParseAttempt:
    parsed: ParsedEdition | None
    mode: TitleParseMode
    recovery_kind: str | None = None


def parse_with_recovery(
    provider_id: str,
    title: str,
    strict_parse: StrictTitleParser,
) -> TitleParseAttempt:
    """Parse one title with bounded syntax recovery while preserving the original title."""

    exact = strict_parse(title)
    if exact is not None:
        return TitleParseAttempt(_preserve_original_title(exact, title), "exact")

    normalized = normalize_title_syntax(title)
    if normalized != title.strip():
        parsed = strict_parse(normalized)
        if parsed is not None:
            return TitleParseAttempt(
                _preserve_original_title(parsed, title),
                "normalized",
                "unicode_whitespace_or_dash",
            )

    seen = {title, title.strip(), normalized}
    for candidate, recovery_kind in _provider_compat_candidates(provider_id, normalized):
        if candidate in seen:
            continue
        seen.add(candidate)
        parsed = strict_parse(candidate)
        if parsed is not None:
            return TitleParseAttempt(
                _preserve_original_title(parsed, title),
                "provider_compat",
                recovery_kind,
            )

    return TitleParseAttempt(None, "rejected")


def normalize_title_syntax(title: str) -> str:
    """Normalize syntax-only Unicode/spacing variants without changing semantic tokens."""

    value = unicodedata.normalize("NFKC", title).translate(_ZERO_WIDTH).translate(_DASHES)
    return " ".join(value.split()).strip()


def _provider_compat_candidates(provider_id: str, title: str) -> Iterable[tuple[str, str]]:
    if provider_id == "rne":
        candidate = _canonical_rne(title)
        if candidate is not None:
            yield candidate, "rne_date_time_separators"
    elif provider_id == "ser":
        candidate = _canonical_ser(title)
        if candidate is not None:
            yield candidate, "ser_layout_punctuation"
    elif provider_id == "ondacero":
        candidate = _canonical_ondacero(title)
        if candidate is not None:
            yield candidate, "ondacero_layout_punctuation"


def _canonical_rne(title: str) -> str | None:
    match = _RNE_COMPAT.fullmatch(title)
    if match is None:
        return None
    values = match.groupdict()
    minute = values["minute"] or "00"
    return (
        f'{values["product"]} - {values["day"]}.{values["month"]}.{values["year"]} - '
        f'{values["hour"]}.{minute}H'
    )


def _canonical_ser(title: str) -> str | None:
    match = _SER_COMPAT.fullmatch(title)
    if match is None:
        return None
    values = match.groupdict()
    year = _four_digit_current_year(values["year"])
    return (
        f'Las noticias de la SER, {values["hour"]}:{values["minute"]} '
        f'({values["day"]}/{values["month"]}/{year})'
    )


def _canonical_ondacero(title: str) -> str | None:
    match = _ONDACERO_COMPAT.fullmatch(title)
    if match is None:
        return None
    values = match.groupdict()
    year = _four_digit_current_year(values["year"])
    return (
        f'Las noticias de Onda Cero de las {values["hour"]}:{values["minute"]}h '
        f'({values["day"]}/{values["month"]}/{year})'
    )


def _four_digit_current_year(value: str) -> str:
    return value if len(value) == 4 else f"20{value}"


def _preserve_original_title(parsed: ParsedEdition, original_title: str) -> ParsedEdition:
    if parsed.title == original_title:
        return parsed
    return ParsedEdition(parsed.provider_id, original_title, parsed.edition_at)
