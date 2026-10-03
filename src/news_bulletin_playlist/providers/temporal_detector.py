"""Bounded semantic temporal evidence, independent of source identity.

Configured capabilities and corroborated catalogue structures establish edition semantics.
Token discovery alone never authorizes a timestamp. All routes validate the official
release period and resolve local wall time through the common DST-safe primitive.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import ParsedEdition, TemporalStrategy
from news_bulletin_playlist.providers.base import CatalogueParseContext
from news_bulletin_playlist.providers.common import (
    SPANISH_MONTH_NAMES,
    clock_12h_pattern,
    datetime_from_match,
    named_month_date_pattern,
    numeric_date_pattern,
    parse_24h_clock_value,
    parse_numeric_date_value,
    spanish_month,
    unique_aware_datetime,
)
from news_bulletin_playlist.providers.title_recovery import (
    MAX_TITLE_PARSE_CHARS,
    TemporalEvidenceKind,
    TitleParseAttempt,
    TitleParseMode,
    normalize_title_syntax,
)
from news_bulletin_playlist.spotify.first_models import release_period

MAX_CATALOGUE_SAMPLE = 200
MIN_STRUCTURE_ROWS = 5

# These patterns discover *possible* tokens, including invalid values. Keeping invalid
# and duplicate tokens prevents a valid substring from hiding conflicting evidence.
_NUMERIC_DATE = re.compile(
    r"(?<![\w./])(?:[0-9]{1,4}[./-][0-9]{1,2}[./-][0-9]{2,4}|[0-9]{6,8})(?![\w/])"
)
_ENGLISH_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTHS = {name.casefold(): index for index, name in enumerate(_ENGLISH_MONTHS, 1)}
_NAMED_DATE = re.compile(
    r"(?<!\w)"
    + named_month_date_pattern(order="dmy", month_names=(*_ENGLISH_MONTHS, *SPANISH_MONTH_NAMES))
    + r"(?!\w)",
    re.IGNORECASE | re.ASCII,
)
_CLOCK_TOKEN = re.compile(
    r"(?<![\w:.,])(?:[0-9]{1,2}(?:[:.,h][0-9]{1,2})?\s*(?:am|pm)"
    r"|[0-9]{1,2}[:.,h][0-9]{1,2}(?:\s*[hH])?"
    r"|[0-9]{1,4}\s*[hH])(?![\w:]|[.,][0-9])",
    re.IGNORECASE | re.ASCII,
)
_CLOCK_12 = re.compile(
    clock_12h_pattern(separators=":", minute_optional=True) + r"\s*(?P<ampm>am|pm)",
    re.IGNORECASE | re.ASCII,
)


@dataclass(frozen=True, slots=True)
class TemporalSample:
    title: str
    context: CatalogueParseContext


@dataclass(frozen=True, slots=True)
class _Tokens:
    title: str
    dates: tuple[re.Match[str], ...]
    clocks: tuple[re.Match[str], ...]

    @property
    def signature(self) -> str | None:
        if len(self.dates) != 1 or len(self.clocks) != 1:
            return None
        tokens = sorted(
            [(self.dates[0], "{date}"), (self.clocks[0], "{clock}")],
            key=lambda item: item[0].start(),
        )
        first, last = tokens[0][0], tokens[1][0]
        # Edition fields must be adjacent, without narrative between them. A leading
        # block may have a pipe-delimited label instead of a prefix.
        between = self.title[first.end() : last.start()]
        suffix = self.title[last.end() :]
        if re.fullmatch(r"[\s,:|;()\[\].-]+", between) is None:
            return None
        trailing_label = not self.title[: first.start()].strip() and re.fullmatch(
            r"\s*\|\s*[^\W\d_][^\d|:;()\[\]]{0,60}", suffix
        )
        if not trailing_label and (
            re.fullmatch(r"(?:\s*(?:Uhr|GMT|UTC))?[\s).\]]*", suffix, re.ASCII | re.IGNORECASE)
            is None
        ):
            return None
        prefix = self.title[: first.start()]
        if (
            (not prefix.strip() and not trailing_label)
            or len(prefix.split()) > 12
            or re.search(r"[0-9]{2,}", prefix)
        ):
            return None
        brackets = "".join(character for character in self.title if character in "()[]")
        if brackets not in {"", "()", "[]"}:
            return None
        clock_form = re.sub(r"[0-9]+", "#", self.clocks[0].group().casefold())
        clock_form = re.sub(r"[ap]m$", "meridiem", clock_form)
        date_form = re.sub(r"[0-9]+", "#", self.dates[0].group().casefold())
        return (prefix + tokens[0][1] + between + tokens[1][1] + suffix).casefold() + (
            "|" + date_form + "|" + clock_form
        )


def _safe_title(title: str) -> str | None:
    if len(title) > MAX_TITLE_PARSE_CHARS:
        return None
    normalized = normalize_title_syntax(title)
    # Reject compatibility/confusable forms rather than translating them into evidence.
    if any(c in "İıſK" or (c.isnumeric() and c not in "0123456789") for c in title + normalized):
        return None
    if unicodedata.normalize("NFKC", normalized) != normalized:
        return None
    if any(unicodedata.category(c).startswith("C") for c in normalized):
        return None
    return normalized


def _tokens(title: str) -> _Tokens:
    dates = tuple(
        sorted(
            (*_NUMERIC_DATE.finditer(title), *_NAMED_DATE.finditer(title)),
            key=lambda match: match.start(),
        )
    )
    clocks = tuple(
        match
        for match in _CLOCK_TOKEN.finditer(title)
        if not any(match.start() < day.end() and day.start() < match.end() for day in dates)
    )
    return _Tokens(title, dates, clocks)


def _clock(token: str) -> tuple[int, int] | None:
    match = _CLOCK_12.fullmatch(token)
    if match is not None:
        # Reuse the shared meridiem conversion and validation, using a neutral date.
        full = re.fullmatch(
            numeric_date_pattern(order="ymd", separators="-") + " " + _CLOCK_12.pattern,
            "2000-01-01 " + token,
            re.IGNORECASE | re.ASCII,
        )
        assert full is not None
        value = datetime_from_match(full, tz=ZoneInfo("UTC"), clock="12h")
        return None if value is None else (value.hour, value.minute)
    return parse_24h_clock_value(
        token.rstrip("hH "),
        separators=":.,hH",
        compact=True,
        minute_optional=token.lower().endswith("h") or token.isascii() and token.isdigit(),
    )


def _date_options(token: str) -> dict[str, set[date]]:
    named = _NAMED_DATE.fullmatch(token)
    if named is not None:
        month = _MONTHS.get(named.group("month").casefold()) or spanish_month(named.group("month"))
        try:
            return {"named": {date(int(named.group("year")), month or 0, int(named.group("day")))}}
        except ValueError:
            return {}
    results: dict[str, set[date]] = {}
    for order in ("dmy", "mdy", "ymd"):
        for policy in ("pivot", "2000"):
            value = parse_numeric_date_value(
                token,
                order=order,
                separators="./-",
                year_digits=(4, 2),
                compact=True,
                two_digit_year_policy=policy,
            )
            if value is not None:
                results.setdefault(order, set()).add(value)
    return results


def _dates(token: str, orders: frozenset[str] | None = None) -> set[date]:
    return {
        day
        for order, days in _date_options(token).items()
        if orders is None or order in orders
        for day in days
    }


def _period(context: CatalogueParseContext) -> tuple[date, date] | None:
    try:
        period = release_period(context.release_date, context.release_date_precision)
    except ValueError:
        return None
    expected = {"day": 10, "month": 7, "year": 4}.get(context.release_date_precision)
    if expected is None or context.release_date != period[0].isoformat()[:expected]:
        return None
    return period


def _resolve(
    tokens: _Tokens,
    context: CatalogueParseContext,
    *,
    recover_date: bool,
    orders: frozenset[str] | None = None,
) -> datetime | None:
    period = _period(context)
    if (
        context.timezone is None
        or period is None
        or len(tokens.clocks) != 1
        or len(tokens.dates) != 1
    ):
        return None
    clock = _clock(tokens.clocks[0].group())
    if clock is None:
        return None
    days = _dates(tokens.dates[0].group(), orders)
    compatible = {day for day in days if period[0] <= day <= period[1]}
    if len(compatible) == 1:
        day = next(iter(compatible))
    elif (
        not _dates(tokens.dates[0].group())
        and recover_date
        and context.release_date_precision == "day"
    ):
        day = period[0]
    else:
        return None
    value = unique_aware_datetime(day.year, day.month, day.day, *clock, tz=context.timezone)
    return value if value is not None and _zone_agrees(tokens.title, value) else None


def _zone_agrees(title: str, value: datetime) -> bool:
    return not re.search(r"\b(?:GMT|UTC)\b", title, re.IGNORECASE) or not value.utcoffset()


def _override(
    tokens: _Tokens,
    context: CatalogueParseContext,
    strategy: TemporalStrategy,
    *,
    orders: frozenset[str] | None = None,
) -> tuple[datetime | None, bool]:
    """A configured capability attests edition semantics, never calendar validity.

    These bounded field layouts contain no product names. A clock contract cannot
    consume a full date, and a datetime contract cannot manufacture a missing date.
    """
    if strategy in {TemporalStrategy.AUTO, TemporalStrategy.DISABLED}:
        return None, False
    period = _period(context)
    if period is None or context.timezone is None:
        return None, False
    if not tokens.clocks and len(tokens.dates) == 1 and strategy is TemporalStrategy.FULL_DATETIME:
        # An explicit full-datetime contract may use a terminal bare hour/HHMM field.
        # AUTO never discovers these unmarked numbers as clocks.
        bare = re.search(r"(?<=[ -])[0-9]{1,4}$", tokens.title)
        if bare is not None and bare.start() > tokens.dates[0].end():
            tokens = _Tokens(tokens.title, tokens.dates, (bare,))
    if len(tokens.clocks) != 1:
        return None, False
    if strategy in {TemporalStrategy.FULL_DATETIME, TemporalStrategy.EXPLICIT_UTC} or (
        strategy is TemporalStrategy.PARTIAL_DATE_CATALOGUE_DAY and tokens.dates
    ):
        if strategy is TemporalStrategy.EXPLICIT_UTC and not re.search(
            r"\b(?:GMT|UTC)$", tokens.title, re.IGNORECASE
        ):
            return None, False
        if tokens.signature is None:
            return None, False
        return _resolve(tokens, context, recover_date=False, orders=orders), False
    if context.release_date_precision != "day":
        return None, True
    clock_token = tokens.clocks[0]
    clock = _clock(clock_token.group())
    if clock is None:
        return None, True
    day = period[0]
    if strategy is TemporalStrategy.CLOCK_CATALOGUE_DAY:
        if tokens.dates:
            # A malformed compact date is never parsed. Only its isolated slot and
            # the explicitly configured clock contract authorize catalogue recovery.
            if (
                len(tokens.dates) != 1
                or re.fullmatch(r"[0-9]{7}", tokens.dates[0].group()) is None
                or tokens.signature is None
            ):
                return None, True
        else:
            # Only a leading clock followed by a short nonnumeric product label.
            label = tokens.title[clock_token.end() :].strip()
            if clock_token.start() != 0 or not label or len(label.split()) > 8:
                return None, True
            if any(c.isnumeric() for c in label) or re.search(r"[^\w\s'-]", label):
                return None, True
    elif strategy is TemporalStrategy.PARTIAL_DATE_CATALOGUE_DAY:
        partial = re.search(r"(?<![\w/])[0-9]{1,2}/[0-9]{1,2}(?![\w/])", tokens.title)
        if partial is None or partial.end() >= clock_token.start():
            return None, True
        prefix = tokens.title[: partial.start()].strip()
        between = tokens.title[partial.end() : clock_token.start()]
        suffix = tokens.title[clock_token.end() :]
        if (
            not prefix
            or any(c.isnumeric() for c in prefix)
            or not re.fullmatch(r"[\s,|;-]+", between)
            or not re.fullmatch(r"\s*(?:GMT|UTC)?\s*", suffix, re.IGNORECASE)
        ):
            return None, True
        possible = {
            parsed
            for order in ("dmy", "mdy")
            if (
                parsed := parse_numeric_date_value(
                    f"{partial.group()}/{day.year}", order=order, separators="/", year_digits=(4,)
                )
            )
            is not None
        }
        if day not in possible:
            return None, True
    return unique_aware_datetime(day.year, day.month, day.day, *clock, tz=context.timezone), True


def _can_use_configured_complete_date_after_signature_conflict(
    tokens: _Tokens, strategy: TemporalStrategy
) -> bool:
    """Allow only row-local complete-date capabilities past a sibling conflict."""
    return (
        strategy
        in {
            TemporalStrategy.FULL_DATETIME,
            TemporalStrategy.PARTIAL_DATE_CATALOGUE_DAY,
            TemporalStrategy.EXPLICIT_UTC,
        }
        and len(tokens.dates) == 1
        and bool(_dates(tokens.dates[0].group()))
    )


class TemporalDetector:
    """One detector per fetched catalogue, never persisted or trained across sources."""

    def __init__(
        self,
        samples: Sequence[TemporalSample],
        strategy: TemporalStrategy = TemporalStrategy.AUTO,
    ) -> None:
        self.strategy = TemporalStrategy(strategy)
        self.sample_count = len(samples)
        self.conflicts = 0
        self._structures: dict[str, tuple[frozenset[str], int]] = {}
        self._conflicted_signatures: set[str] = set()
        self._conflicted_signature_orders: dict[str, frozenset[str]] = {}
        if (
            self.strategy is TemporalStrategy.DISABLED
            or not samples
            or len(samples) > MAX_CATALOGUE_SAMPLE
        ):
            return
        groups: dict[str, list[tuple[_Tokens, CatalogueParseContext]]] = {}
        for sample in samples:
            title = _safe_title(sample.title)
            if title is None:
                continue
            tokens = _tokens(title)
            signature = tokens.signature
            if signature is not None:
                groups.setdefault(signature, []).append((tokens, sample.context))
        for signature, rows in groups.items():
            proven = [
                (tokens, context, _resolve(tokens, context, recover_date=False))
                for tokens, context in rows
            ]
            order_witnesses = [
                (tokens, context, value) for tokens, context, value in proven if value is not None
            ]
            auto_valid = [
                (tokens, context, value)
                for tokens, context, value in order_witnesses
                if context.release_date_precision == "day"
            ]
            # No valid-but-contradictory calendar or invalid clock may train a structure.
            contradiction = any(
                value is None and bool(_dates(tokens.dates[0].group()))
                for tokens, _, value in proven
            )
            auto_orders = {"dmy", "mdy", "ymd", "named"}
            witness_orders = auto_orders.copy()
            for tokens, context, value in order_witnesses:
                assert value is not None
                compatible_orders = {
                    order
                    for order, days in _date_options(tokens.dates[0].group()).items()
                    if value.date() in days
                }
                witness_orders.intersection_update(compatible_orders)
                if context.release_date_precision == "day":
                    auto_orders.intersection_update(compatible_orders)
            if contradiction or auto_valid and not auto_orders:
                self._conflicted_signatures.add(signature)
                self.conflicts += max(1, sum(value is None for _, _, value in proven))
                if contradiction and order_witnesses and witness_orders:
                    self._conflicted_signature_orders[signature] = frozenset(witness_orders)
            if (
                auto_orders
                and not contradiction
                and len({(tokens.title, context.release_date) for tokens, context, _ in auto_valid})
                >= MIN_STRUCTURE_ROWS
                and len(auto_valid) * 5 >= len(samples) * 4
                and len({_clock(tokens.clocks[0].group()) for tokens, _, _ in auto_valid}) >= 3
            ):
                self._structures[signature] = (frozenset(auto_orders), len(auto_valid))

    @property
    def supporting_rows(self) -> int:
        return max((count for _, count in self._structures.values()), default=0)

    def parse(self, title: str, *, context: CatalogueParseContext) -> TitleParseAttempt:
        if self.strategy is TemporalStrategy.DISABLED:
            return TitleParseAttempt(None, "rejected", temporal_evidence="disabled")
        normalized = _safe_title(title)
        period = _period(context)
        if normalized is None or period is None:
            return TitleParseAttempt(
                None, "rejected", temporal_evidence="invalid_context_or_syntax"
            )
        tokens = _tokens(normalized)
        if context.timezone is None:
            if not tokens.clocks and not tokens.dates:
                return TitleParseAttempt(None, "rejected", temporal_evidence="unproven_structure")
            return TitleParseAttempt(
                None, "rejected", temporal_evidence="invalid_context_or_syntax"
            )
        if len(tokens.clocks) > 1 or len(tokens.dates) > 1:
            return TitleParseAttempt(
                None, "rejected", temporal_evidence="multiple_temporal_candidates"
            )

        # A learned structure is restricted to the exact surrounding text/positions.
        # A damaged date is recoverable only after other rows prove that same structure.
        signature = tokens.signature
        if signature in self._conflicted_signatures:
            orders = self._conflicted_signature_orders.get(signature)
            if orders is not None and _can_use_configured_complete_date_after_signature_conflict(
                tokens, self.strategy
            ):
                configured_value, configured_catalogue_day = _override(
                    tokens,
                    context,
                    self.strategy,
                    orders=orders,
                )
                if configured_value is not None and _zone_agrees(normalized, configured_value):
                    return self._attempt(
                        title,
                        configured_value,
                        "catalogue_context"
                        if configured_catalogue_day
                        else "normalized"
                        if normalized != title.strip()
                        else "exact",
                        evidence="configured_capability",
                    )
            return TitleParseAttempt(None, "rejected", temporal_evidence="catalogue_date_conflict")
        if signature is not None and signature in self._structures:
            orders, count = self._structures[signature]
            value = _resolve(tokens, context, recover_date=True, orders=orders)
            if value is not None and _zone_agrees(normalized, value):
                return self._attempt(
                    title,
                    value,
                    "catalogue_context"
                    if not _dates(tokens.dates[0].group())
                    else "normalized"
                    if normalized != title.strip()
                    else "exact",
                    evidence="catalogue_structure",
                    supporting_rows=count,
                )
            return TitleParseAttempt(None, "rejected", temporal_evidence="catalogue_date_conflict")
        configured_value, configured_catalogue_day = _override(tokens, context, self.strategy)
        if configured_value is not None and _zone_agrees(normalized, configured_value):
            return self._attempt(
                title,
                configured_value,
                "catalogue_context"
                if configured_catalogue_day
                else "normalized"
                if normalized != title.strip()
                else "exact",
                evidence="configured_capability",
            )
        return TitleParseAttempt(None, "rejected", temporal_evidence="unproven_structure")

    @staticmethod
    def _attempt(
        title: str,
        value: datetime,
        mode: TitleParseMode,
        *,
        evidence: TemporalEvidenceKind,
        supporting_rows: int = 0,
    ) -> TitleParseAttempt:
        return TitleParseAttempt(
            ParsedEdition("universal", title, value),
            mode,
            temporal_evidence=evidence,
            supporting_rows=supporting_rows,
        )
