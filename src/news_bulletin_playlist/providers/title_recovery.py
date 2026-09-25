from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.common import clock_24h_pattern, numeric_date_pattern

type TitleParseMode = Literal[
    "exact",
    "normalized",
    "provider_compat",
    "catalogue_context",
    "rejected",
]
type StrictTitleParser = Callable[[str], ParsedEdition | None]
type CompatCanonicalizer = Callable[[str], str | None]

# Current bulletin contracts produce short human-readable titles. 1024 characters is
# intentionally far above every accepted grammar while bounding regex/Unicode work on
# malformed provider input before the strict parser gets a chance to backtrack.
MAX_TITLE_PARSE_CHARS = 1024

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
# Python Unicode case-insensitive regex matching treats these non-ASCII characters as
# equivalents of ASCII I/i/S/K. None belongs to the reviewed provider grammars, so reject
# them before the historical IGNORECASE regex can broaden product identity.
_UNICODE_IGNORECASE_CONFUSABLES = frozenset({"\u0130", "\u0131", "\u017f", "\u212a"})

_COMPAT_DMY_DATE = numeric_date_pattern(order="dmy", separators="./-", year_digits=(2, 4))
_COMPAT_CLOCK = clock_24h_pattern(separators=".:,")
_RNE_COMPAT_CLOCK = clock_24h_pattern(separators=".:,", minute_optional=True)

_RNE_COMPAT = re.compile(
    r"^(?P<product>NOTICIAS\s+RNE|RNE\s+NOTICIAS)\s*(?:[-|:]\s*)?"
    rf"{_COMPAT_DMY_DATE}\.?"
    rf"(?:\s+|\s*[-|:]\s*){_RNE_COMPAT_CLOCK}\s*H?\.?$",
    re.IGNORECASE,
)
_SER_COMPAT = re.compile(
    r"^Las\s+noticias\s+de\s+la\s+SER,?\s*(?:[-|:]\s*)?"
    rf"{_COMPAT_CLOCK}\s+"
    rf"(?P<open>[\(\[])?\s*{_COMPAT_DMY_DATE}"
    r"\s*(?P<close>[\)\]])?\.?$",
    re.IGNORECASE,
)
_ONDACERO_COMPAT = re.compile(
    r"^Las\s+noticias\s+de\s+Onda\s+Cero\s+de\s+las\s+"
    rf"{_COMPAT_CLOCK}\s*[hH]?\s+"
    rf"(?P<open>[\(\[])?\s*{_COMPAT_DMY_DATE}"
    r"\s*(?P<close>[\)\]])?\.?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TitleParseAttempt:
    parsed: ParsedEdition | None
    mode: TitleParseMode
    recovery_kind: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderCompatProfile:
    """Provider-specific syntax compatibility that still delegates final validation."""

    canonicalize: CompatCanonicalizer
    recovery_kind: str


def parse_with_recovery(
    provider_id: str,
    title: str,
    strict_parse: StrictTitleParser,
) -> TitleParseAttempt:
    """Parse one title with bounded syntax recovery while preserving the original title."""

    if len(title) > MAX_TITLE_PARSE_CHARS:
        return TitleParseAttempt(None, "rejected", "title_too_long")
    if _contains_unicode_ignorecase_confusable(title):
        return TitleParseAttempt(None, "rejected", "unicode_casefold_confusable")

    exact = strict_parse(title)
    if exact is not None:
        return TitleParseAttempt(_preserve_original_title(exact, title), "exact")

    normalized = normalize_title_syntax(title)
    if _contains_unicode_ignorecase_confusable(normalized):
        return TitleParseAttempt(None, "rejected", "unicode_casefold_confusable")
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


def _contains_unicode_ignorecase_confusable(title: str) -> bool:
    if any(character in _UNICODE_IGNORECASE_CONFUSABLES for character in title):
        return True
    composed = unicodedata.normalize("NFC", title)
    return any(character in _UNICODE_IGNORECASE_CONFUSABLES for character in composed)


def normalize_title_syntax(title: str) -> str:
    """Normalize only reviewed syntax variants, never compatibility-fold product text."""

    # NFC composes canonically equivalent Unicode while intentionally avoiding NFKC.
    # Compatibility folding would turn full-width letters/digits or superscripts into
    # ordinary product/date tokens and could silently broaden provider identity.
    value = unicodedata.normalize("NFC", title).translate(_ZERO_WIDTH).translate(_DASHES)
    collapsed = " ".join(value.split()).strip()
    # Removing an allowed zero-width character can create a new canonical composition
    # boundary (for example ASCII I + combining dot). Compose once more so the caller's
    # post-normalization confusable guard sees the actual candidate fed to strict parsing.
    return unicodedata.normalize("NFC", collapsed)


def _provider_compat_candidates(provider_id: str, title: str) -> Iterable[tuple[str, str]]:
    if provider_id == "rne" and title.endswith("."):
        # Let the historical strict parser decide whether the otherwise-identical compact
        # form is valid. This intentionally does not create a second compact grammar.
        terminal_candidate = title[:-1].rstrip()
        if terminal_candidate:
            yield terminal_candidate, "rne_terminal_punctuation"

    profile = _PROVIDER_COMPAT_PROFILES.get(provider_id)
    if profile is None:
        return
    canonical_candidate = profile.canonicalize(title)
    if canonical_candidate is not None:
        yield canonical_candidate, profile.recovery_kind


def _canonical_rne(title: str) -> str | None:
    match = _RNE_COMPAT.fullmatch(title)
    if match is None:
        return None
    values = match.groupdict()
    minute = values["minute"] or "00"
    return (
        f"{values['product']} - {values['day']}.{values['month']}.{values['year']} - "
        f"{values['hour']}.{minute}H"
    )


def _canonical_ser(title: str) -> str | None:
    match = _SER_COMPAT.fullmatch(title)
    if match is None:
        return None
    values = match.groupdict()
    if not _balanced_optional_brackets(values["open"], values["close"]):
        return None
    year = _four_digit_spanish_year(values["year"])
    return (
        f"Las noticias de la SER, {values['hour']}:{values['minute']} "
        f"({values['day']}/{values['month']}/{year})"
    )


def _canonical_ondacero(title: str) -> str | None:
    match = _ONDACERO_COMPAT.fullmatch(title)
    if match is None:
        return None
    values = match.groupdict()
    if not _balanced_optional_brackets(values["open"], values["close"]):
        return None
    year = _four_digit_spanish_year(values["year"])
    return (
        f"Las noticias de Onda Cero de las {values['hour']}:{values['minute']}h "
        f"({values['day']}/{values['month']}/{year})"
    )


def _balanced_optional_brackets(open_bracket: str | None, close_bracket: str | None) -> bool:
    return (open_bracket, close_bracket) in {
        (None, None),
        ("(", ")"),
        ("[", "]"),
    }


def _four_digit_spanish_year(value: str) -> str:
    # RNE already used the 2000+YY rule historically; SER/Onda compatibility intentionally
    # follows the same reviewed bulletin-era policy rather than a clock-dependent pivot.
    return value if len(value) == 4 else f"20{value}"


def _preserve_original_title(parsed: ParsedEdition, original_title: str) -> ParsedEdition:
    if parsed.title == original_title:
        return parsed
    return ParsedEdition(parsed.provider_id, original_title, parsed.edition_at)


_PROVIDER_COMPAT_PROFILES: dict[str, ProviderCompatProfile] = {
    "rne": ProviderCompatProfile(_canonical_rne, "rne_date_time_separators"),
    "ser": ProviderCompatProfile(_canonical_ser, "ser_layout_punctuation"),
    "ondacero": ProviderCompatProfile(_canonical_ondacero, "ondacero_layout_punctuation"),
}
