from __future__ import annotations

from datetime import datetime

import pytest

from news_bulletin_playlist.providers.cnn import CnnParser
from news_bulletin_playlist.providers.common import MADRID, NEW_YORK
from news_bulletin_playlist.providers.ondacero import OndaCeroParser
from news_bulletin_playlist.providers.rne import RneParser
from news_bulletin_playlist.providers.ser import SerParser
from news_bulletin_playlist.providers.title_recovery import (
    MAX_TITLE_PARSE_CHARS,
    parse_with_recovery,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (
            "NOTICIAS RNE - 17.09.2026 - 0930H.",
            datetime(2026, 9, 17, 9, 30, tzinfo=MADRID),
        ),
        (
            "NOTICIAS RNE - 170926 - 09.30H.",
            datetime(2026, 9, 17, 9, 30, tzinfo=MADRID),
        ),
    ],
)
def test_rne_compact_historical_forms_recover_terminal_period(
    title: str,
    expected: datetime,
) -> None:
    attempt = RneParser().parse_with_recovery(title)

    assert attempt.mode == "provider_compat"
    assert attempt.recovery_kind == "rne_terminal_punctuation"
    assert attempt.parsed is not None
    assert attempt.parsed.title == title
    assert attempt.parsed.edition_at == expected


@pytest.mark.parametrize(
    "title",
    [
        "NOTICIAS RNE - 170926.",
        "NOTICIAS RNE - 170926 - 09.30H. - EXTRA.",
        "RNE DEPORTES - 170926 - 09.30H.",
    ],
)
def test_rne_terminal_period_recovery_does_not_broaden_semantics(title: str) -> None:
    attempt = RneParser().parse_with_recovery(title)

    assert attempt.mode == "rejected"
    assert attempt.parsed is None


def test_title_length_guard_runs_before_strict_parser() -> None:
    calls = 0

    def strict_parser(_title: str):
        nonlocal calls
        calls += 1
        return None

    attempt = parse_with_recovery("test", "X" * (MAX_TITLE_PARSE_CHARS + 1), strict_parser)

    assert attempt.mode == "rejected"
    assert attempt.recovery_kind == "title_too_long"
    assert calls == 0


def test_title_length_guard_boundary_still_uses_strict_parser() -> None:
    calls = 0

    def strict_parser(_title: str):
        nonlocal calls
        calls += 1
        return None

    attempt = parse_with_recovery("test", "X" * MAX_TITLE_PARSE_CHARS, strict_parser)

    assert attempt.mode == "rejected"
    assert calls >= 1


def test_rne_pathological_whitespace_is_bounded_by_public_recovery() -> None:
    title = "NOTICIAS RNE" + (" " * 16_000) + "X"

    attempt = RneParser().parse_with_recovery(title)

    assert attempt.mode == "rejected"
    assert attempt.recovery_kind == "title_too_long"


@pytest.mark.parametrize(
    ("parser", "canonical", "mutated"),
    [
        (
            RneParser(),
            "NOTICIAS RNE - 17.09.2026 - 09.30H",
            "NOTICIAS\u00a0RNE\u00a0–\u00a017.09.2026\u00a0–\u00a009.30H",
        ),
        (
            SerParser(),
            "Las noticias de la SER, 09:30 (17/09/2026)",
            "Las noticias de la SER - 9.30 17-09-26",
        ),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 09:30h (17/09/2026)",
            "Las noticias de Onda Cero de las 9.30 H 17-09-26.",
        ),
    ],
)
def test_reviewed_safe_mutations_keep_semantic_time_and_original_title(
    parser: RneParser | SerParser | OndaCeroParser,
    canonical: str,
    mutated: str,
) -> None:
    baseline = parser.parse(canonical)
    attempt = parser.parse_with_recovery(mutated)

    assert baseline is not None
    assert attempt.parsed is not None
    assert attempt.mode in {"normalized", "provider_compat"}
    assert attempt.parsed.edition_at == baseline.edition_at
    assert attempt.parsed.title == mutated
    assert attempt.parsed.edition_at.tzinfo is not None
    assert parser.parse(mutated) == attempt.parsed


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (RneParser(), "RNE DEPORTES | 17/09/26 : 09:30 H."),
        (SerParser(), "SER DEPORTES - 9.30 17-09-26"),
        (OndaCeroParser(), "Onda Cero Deportes de las 9.30 H 17-09-26."),
        (RneParser(), "PROMO NOTICIAS RNE | 17/09/26 : 09:30 H."),
        (SerParser(), "Las noticias de la SER - 9.30 17-09-26 - ESPECIAL"),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 9.30 H 17-09-26. DEPORTES",
        ),
    ],
)
def test_semantic_mutations_never_broaden_product_identity(
    parser: RneParser | SerParser | OndaCeroParser,
    title: str,
) -> None:
    attempt = parser.parse_with_recovery(title)

    assert attempt.mode == "rejected"
    assert attempt.parsed is None


@pytest.mark.parametrize(
    ("year", "expected_year"),
    [("00", 2000), ("26", 2026), ("68", 2068), ("69", 2069), ("70", 2070), ("99", 2099)],
)
def test_spanish_compat_short_year_policy_is_explicit(year: str, expected_year: int) -> None:
    titles = (
        (RneParser(), f"NOTICIAS RNE | 17/09/{year} : 09:30 H."),
        (SerParser(), f"Las noticias de la SER - 9.30 17-09-{year}"),
        (OndaCeroParser(), f"Las noticias de Onda Cero de las 9.30 H 17-09-{year}."),
    )

    for parser, title in titles:
        attempt = parser.parse_with_recovery(title)
        assert attempt.parsed is not None
        assert attempt.parsed.edition_at.year == expected_year


@pytest.mark.parametrize(
    ("year", "expected_year"),
    [("00", 2000), ("26", 2026), ("68", 2068), ("69", 1969), ("70", 1970), ("99", 1999)],
)
def test_cnn_historical_short_year_pivot_remains_unchanged(year: str, expected_year: int) -> None:
    parsed = CnnParser().parse(f"CNN 5 cosas 09/17/{year} 5:00 am")

    assert parsed is not None
    assert parsed.edition_at.year == expected_year
    assert parsed.edition_at.tzinfo == NEW_YORK


def test_recovery_preserves_dst_interpretation_of_exact_parser() -> None:
    exact = SerParser().parse("Las noticias de la SER, 02:30 (25/10/2026)")
    recovered = SerParser().parse_with_recovery("Las noticias de la SER - 2.30 25-10-26")

    assert exact is not None
    assert recovered.parsed is not None
    assert recovered.parsed.edition_at == exact.edition_at
    assert recovered.parsed.edition_at.fold == exact.edition_at.fold
