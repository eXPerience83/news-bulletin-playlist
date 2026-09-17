from __future__ import annotations

from datetime import datetime

import pytest

from news_bulletin_playlist.provider_watch import evaluate_titles
from news_bulletin_playlist.providers.common import MADRID
from news_bulletin_playlist.providers.ondacero import OndaCeroParser
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
def test_exact_titles_keep_exact_mode(parser: object, title: str, expected: datetime) -> None:
    attempt = parser.parse_with_recovery(title)  # type: ignore[attr-defined]

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
    ("parser", "title", "expected", "kind"),
    [
        (
            RneParser(),
            "NOTICIAS RNE | 17/09/26 : 09:30 H.",
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
    parser: object,
    title: str,
    expected: datetime,
    kind: str,
) -> None:
    attempt = parser.parse_with_recovery(title)  # type: ignore[attr-defined]

    assert attempt.mode == "provider_compat"
    assert attempt.recovery_kind == kind
    assert attempt.parsed is not None
    assert attempt.parsed.title == title
    assert attempt.parsed.edition_at == expected


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
    parser: object,
    title: str,
) -> None:
    attempt = parser.parse_with_recovery(title)  # type: ignore[attr-defined]

    assert attempt.mode == "rejected"
    assert attempt.parsed is None


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
