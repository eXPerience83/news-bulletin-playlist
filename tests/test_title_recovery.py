from __future__ import annotations

from datetime import datetime

import pytest

from news_bulletin_playlist.collection import normalize_rss_source
from news_bulletin_playlist.models import (
    CountryCode,
    LanguageTag,
    ParserId,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.provider_watch import evaluate_titles
from news_bulletin_playlist.providers.base import RecoveryTitleParser
from news_bulletin_playlist.providers.cnn import CnnParser
from news_bulletin_playlist.providers.common import MADRID, NEW_YORK
from news_bulletin_playlist.providers.cope import CopeParser
from news_bulletin_playlist.providers.ondacero import OndaCeroParser
from news_bulletin_playlist.providers.rfi import UTC, RfiJournalMondeParser
from news_bulletin_playlist.providers.rne import RneParser
from news_bulletin_playlist.providers.ser import SerParser


@pytest.mark.parametrize(
    ("parser", "title", "expected"),
    [
        (
            RneParser(),
            "NOTICIAS RNE - 17.09.2026 - 09.30H",
            datetime(2026, 9, 17, 9, 30, tzinfo=MADRID),
        ),
        (
            SerParser(),
            "Las noticias de la SER, 09:30 (17/09/2026)",
            datetime(2026, 9, 17, 9, 30, tzinfo=MADRID),
        ),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 09:30h (17/09/2026)",
            datetime(2026, 9, 17, 9, 30, tzinfo=MADRID),
        ),
    ],
)
def test_exact_titles_keep_exact_mode(
    parser: RecoveryTitleParser,
    title: str,
    expected: datetime,
) -> None:
    attempt = parser.parse_with_recovery(title)

    assert attempt.mode == "exact"
    assert attempt.recovery_kind is None
    assert attempt.parsed is not None
    assert attempt.parsed.title == title
    assert attempt.parsed.edition_at == expected


def test_rne_recovers_unicode_spacing_and_dash_without_changing_original_title() -> None:
    title = "NOTICIAS\u00a0RNE\u00a0–\u00a017.09.2026\u00a0–\u00a009.30H"

    attempt = RneParser().parse_with_recovery(title)

    assert attempt.mode == "normalized"
    assert attempt.recovery_kind == "unicode_whitespace_or_dash"
    assert attempt.parsed is not None
    assert attempt.parsed.title == title
    assert attempt.parsed.edition_at == datetime(2026, 9, 17, 9, 30, tzinfo=MADRID)


@pytest.mark.parametrize(
    ("parser", "title", "expected"),
    [
        (
            CnnParser(),
            "CNN\u00a05 cosas\u00a09/17/26\u00a05:00\u00a0am",
            datetime(2026, 9, 17, 5, 0, tzinfo=NEW_YORK),
        ),
        (
            CopeParser(),
            "09:30H\u200b | \u200b17 SEP 2026\u200b | \u200bBOLETÍN",
            datetime(2026, 9, 17, 9, 30, tzinfo=MADRID),
        ),
        (
            RfiJournalMondeParser(),
            "Journal\u200b 17/09/2026\u200b 09h30\u200b GMT",
            datetime(2026, 9, 17, 9, 30, tzinfo=UTC),
        ),
    ],
)
def test_generic_normalization_applies_to_other_strict_title_parsers(
    parser: RecoveryTitleParser,
    title: str,
    expected: datetime,
) -> None:
    attempt = parser.parse_with_recovery(title)

    assert attempt.mode == "normalized"
    assert attempt.parsed is not None
    assert attempt.parsed.title == title
    assert attempt.parsed.edition_at == expected


@pytest.mark.parametrize(
    ("parser", "title", "expected", "kind"),
    [
        (
            RneParser(),
            "NOTICIAS RNE | 17/09/26 : 09:30 H.",
            datetime(2026, 9, 17, 9, 30, tzinfo=MADRID),
            "rne_date_time_separators",
        ),
        (
            RneParser(),
            "NOTICIAS RNE - 17/09/2026 09:30",
            datetime(2026, 9, 17, 9, 30, tzinfo=MADRID),
            "rne_date_time_separators",
        ),
        (
            RneParser(),
            "NOTICIAS RNE - 17/09/2026. - 09:30",
            datetime(2026, 9, 17, 9, 30, tzinfo=MADRID),
            "rne_date_time_separators",
        ),
        (
            SerParser(),
            "Las noticias de la SER - 9.30 17-09-26",
            datetime(2026, 9, 17, 9, 30, tzinfo=MADRID),
            "ser_layout_punctuation",
        ),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 9.30 H 17-09-26.",
            datetime(2026, 9, 17, 9, 30, tzinfo=MADRID),
            "ondacero_layout_punctuation",
        ),
    ],
)
def test_provider_compat_recovers_safe_layout_variants(
    parser: RecoveryTitleParser,
    title: str,
    expected: datetime,
    kind: str,
) -> None:
    attempt = parser.parse_with_recovery(title)

    assert attempt.mode == "provider_compat"
    assert attempt.recovery_kind == kind
    assert attempt.parsed is not None
    assert attempt.parsed.title == title
    assert attempt.parsed.edition_at == expected


@pytest.mark.parametrize(
    "title",
    [f"NOTICIAS RNE - 17/09/20{suffix:02d}" for suffix in range(24)],
)
def test_rne_date_without_hour_cannot_reuse_year_digits_as_time(title: str) -> None:
    attempt = RneParser().parse_with_recovery(title)

    assert attempt.mode == "rejected"
    assert attempt.parsed is None


@pytest.mark.parametrize(
    "title",
    [
        "NOTICIAS RNE - 17/09/2026¹",
        "NOTICIAS RNE - 17/09/202609",
    ],
)
def test_rne_date_hour_boundary_rejects_concatenated_or_compatibility_digits(title: str) -> None:
    attempt = RneParser().parse_with_recovery(title)

    assert attempt.mode == "rejected"
    assert attempt.parsed is None


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (RneParser(), "RNE DEPORTES - 17.09.2026 - 09.30H"),
        (SerParser(), "SER DEPORTES - 09:30 (17/09/2026)"),
        (OndaCeroParser(), "Onda Cero Deportes 09:30h (17/09/2026)"),
        (RneParser(), "NOTICIAS RNE | 31/02/26 : 09:30 H."),
        (SerParser(), "Las noticias de la SER - 25.30 17-09-26"),
        (OndaCeroParser(), "Las noticias de Onda Cero de las 9.75 H 17-09-26."),
    ],
)
def test_recovery_never_accepts_unrelated_products_or_invalid_date_time(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    attempt = parser.parse_with_recovery(title)

    assert attempt.mode == "rejected"
    assert attempt.parsed is None


def test_rne_date_without_hour_is_not_canonical_or_provider_watch_healthy() -> None:
    source = SourceDefinition(
        id=SourceId("rne"),
        display_name="RNE",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=MADRID,
        enabled=True,
        parser_id=ParserId("rne"),
        endpoint_url="https://example.test/rne.xml",
    )
    title = "NOTICIAS RNE - 17/09/2021"
    payload = f"""\
<rss><channel>
  <item>
    <title>{title}</title>
    <pubDate>Thu, 17 Sep 2026 07:05:00 +0000</pubDate>
    <guid>rne-date-without-hour</guid>
  </item>
</channel></rss>
""".encode()

    with pytest.raises(ValueError, match="no canonical bulletin editions"):
        normalize_rss_source(source, payload)

    result = evaluate_titles("rne", RneParser(), [title] * 6)
    assert result.ok is False
    assert result.parsed == 0
    assert "unresolved 6/6" in result.detail


def test_provider_watch_counts_recovered_titles_as_healthy_without_raw_title_leak() -> None:
    recovered = "NOTICIAS RNE | 17/09/26 : 09:30 H."

    result = evaluate_titles("rne", RneParser(), [recovered] * 6)

    assert result.ok is True
    assert result.parsed == 6
    assert "recovered 6/6" in result.detail
    assert recovered not in result.detail


def test_provider_watch_reports_unresolved_counts_without_echoing_rejected_titles() -> None:
    rejected = "RNE DEPORTES - private-looking arbitrary provider text"

    result = evaluate_titles("rne", RneParser(), [rejected] * 6)

    assert result.ok is False
    assert result.parsed == 0
    assert "unresolved 6/6" in result.detail
    assert rejected not in result.detail
