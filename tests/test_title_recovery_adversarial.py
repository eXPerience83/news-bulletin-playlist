from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.providers.base import RecoveryTitleParser
from news_bulletin_playlist.providers.cnn import CnnParser
from news_bulletin_playlist.providers.cope import CopeParser
from news_bulletin_playlist.providers.ondacero import OndaCeroParser
from news_bulletin_playlist.providers.rfi import RfiJournalMondeParser
from news_bulletin_playlist.providers.rne import RneParser
from news_bulletin_playlist.providers.ser import SerParser
from news_bulletin_playlist.providers.title_recovery import MAX_TITLE_PARSE_CHARS

MADRID = ZoneInfo("Europe/Madrid")
NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


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
            "Las\u00a0noticias de la SER,\u00a009:30\u00a0(17/09/2026)",
        ),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 09:30h (17/09/2026)",
            "Las noticias\u00a0de Onda Cero de las 09:30h\u00a0(17/09/2026)",
        ),
        (
            CnnParser(),
            "CNN 5 cosas 09/17/26 5:00 am",
            "CNN\u00a05 cosas\u00a009/17/26\u00a05:00\u00a0am",
        ),
        (
            CopeParser(),
            "09:30H | 17 SEP 2026 | BOLETÍN",
            "09:30H\u200b | \u200b17 SEP 2026\u200b | \u200bBOLETÍN",
        ),
        (
            RfiJournalMondeParser(),
            "Journal 17/09/2026 09h30 GMT",
            "Journal\u200b 17/09/2026\u200b 09h30\u200b GMT",
        ),
    ],
)
def test_all_strict_providers_recover_reviewed_syntax_without_changing_semantics(
    parser: RecoveryTitleParser,
    canonical: str,
    mutated: str,
) -> None:
    baseline = parser.parse_with_recovery(canonical)
    recovered = parser.parse_with_recovery(mutated)

    assert baseline.mode == "exact"
    assert baseline.parsed is not None
    assert recovered.mode == "normalized"
    assert recovered.parsed is not None
    assert recovered.parsed.edition_at == baseline.parsed.edition_at
    assert recovered.parsed.title == mutated


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (RneParser(), "NOTICIAS ＲＮＥ - 17.09.2026 - 09.30H"),
        (RneParser(), "NOTICIAS RNE | 17/09/２０２６ : 09:30 H."),
        (SerParser(), "Las noticias de la ＳＥＲ, 09:30 (17/09/2026)"),
        (
            OndaCeroParser(),
            "Las noticias de Ｏｎｄａ Cero de las 09:30h (17/09/2026)",
        ),
        (CnnParser(), "ＣＮＮ 5 cosas 09/17/26 5:00 am"),
        (CopeParser(), "09:30H | 17 SEP 2026 | ＢＯＬＥＴÍＮ"),
        (RfiJournalMondeParser(), "Ｊｏｕｒｎａｌ 17/09/2026 09h30 GMT"),
    ],
)
def test_unicode_compatibility_characters_do_not_change_product_identity(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    attempt = parser.parse_with_recovery(title)

    assert attempt.mode == "rejected"
    assert attempt.parsed is None


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (RneParser(), "RNE DEPORTES - 17.09.2026 - 09.30H"),
        (SerParser(), "SER DEPORTES - 09:30 (17/09/2026)"),
        (
            OndaCeroParser(),
            "Onda Cero Deportes de las 09:30h (17/09/2026)",
        ),
        (CnnParser(), "CNN 10 cosas 09/17/26 5:00 am"),
        (CopeParser(), "09:30H | 17 SEP 2026 | BOLETÍN VALENCIA"),
        (
            RfiJournalMondeParser(),
            "Tranche d'information 17/09/2026 09h30 GMT",
        ),
    ],
)
def test_other_programmes_never_recover_as_configured_bulletins(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    assert parser.parse_with_recovery(title).parsed is None


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (RneParser(), "NOTICIAS RNЕ - 17.09.2026 - 09.30H"),
        (SerParser(), "Las noticias de la SЕR, 09:30 (17/09/2026)"),
        (
            OndaCeroParser(),
            "Las noticias de Оnda Cero de las 09:30h (17/09/2026)",
        ),
        (CnnParser(), "СNN 5 cosas 09/17/26 5:00 am"),
        (CopeParser(), "09:30H | 17 SEP 2026 | BОLETÍN"),
        (RfiJournalMondeParser(), "Jоurnal 17/09/2026 09h30 GMT"),
    ],
)
def test_unicode_confusables_do_not_change_product_identity(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    assert parser.parse_with_recovery(title).parsed is None


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (RneParser(), "NOTICIAS RNſ - 17.09.2026 - 09.30H"),
        (SerParser(), "Laſ noticias de la SER, 09:30 (17/09/2026)"),
        (SerParser(), "Las notİcias de la SER, 09:30 (17/09/2026)"),
        (SerParser(), "Las notıcias de la SER, 09:30 (17/09/2026)"),
        (SerParser(), "Las notI\u0307cias de la SER, 09:30 (17/09/2026)"),
        (
            OndaCeroParser(),
            "Las notİcias de Onda Cero de las 09:30h (17/09/2026)",
        ),
    ],
)
def test_python_ignorecase_unicode_extras_cannot_broaden_product_identity(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    attempt = parser.parse_with_recovery(title)

    assert attempt.mode == "rejected"
    assert attempt.recovery_kind == "unicode_casefold_confusable"
    assert attempt.parsed is None


@pytest.mark.parametrize("zero_width", ["\u200b", "\u200c", "\u200d", "\ufeff"])
def test_zero_width_removal_cannot_create_ignorecase_confusable_after_normalization(
    zero_width: str,
) -> None:
    title = f"Las notI{zero_width}\u0307cias de la SER, 09:30 (17/09/2026)"

    attempt = SerParser().parse_with_recovery(title)

    assert attempt.mode == "rejected"
    assert attempt.recovery_kind == "unicode_casefold_confusable"
    assert attempt.parsed is None


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (RneParser(), "NOTICIAS RNE | 17/09/２０２６ : 09:30 H."),
        (SerParser(), "Las noticias de la SER - 9.30 １７-09-26"),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las ９.30 H 17-09-26.",
        ),
        (CnnParser(), "CNN 5 cosas ０9/17/26 5:00 am"),
        (CopeParser(), "０9:30H | 17 SEP 2026 | BOLETÍN"),
        (RfiJournalMondeParser(), "Journal 17/09/2026 ０9h30 GMT"),
    ],
)
def test_unicode_numeric_compatibility_characters_are_not_date_or_time_tokens(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    assert parser.parse_with_recovery(title).parsed is None


@pytest.mark.parametrize(
    ("parser", "canonical"),
    [
        (RneParser(), "NOTICIAS RNE - 17.09.2026 - 09.30H"),
        (SerParser(), "Las noticias de la SER, 09:30 (17/09/2026)"),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 09:30h (17/09/2026)",
        ),
        (CnnParser(), "CNN 5 cosas 09/17/26 5:00 am"),
        (CopeParser(), "09:30H | 17 SEP 2026 | BOLETÍN"),
        (RfiJournalMondeParser(), "Journal 17/09/2026 09h30 GMT"),
    ],
)
def test_all_strict_providers_reject_oversized_titles_before_regex_work(
    parser: RecoveryTitleParser,
    canonical: str,
) -> None:
    oversized = canonical + ("X" * (MAX_TITLE_PARSE_CHARS + 1))

    attempt = parser.parse_with_recovery(oversized)

    assert attempt.mode == "rejected"
    assert attempt.recovery_kind == "title_too_long"
    assert attempt.parsed is None


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (RneParser(), "NOTICIAS RNE | 31/02/26 : 09:30 H."),
        (RneParser(), "NOTICIAS RNE | 17/09/26 : 24:00 H."),
        (SerParser(), "Las noticias de la SER - 9.30 31-02-26"),
        (SerParser(), "Las noticias de la SER - 24.00 17-09-26"),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 9.30 H 31-02-26.",
        ),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 24.00 H 17-09-26.",
        ),
        (CnnParser(), "CNN 5 cosas 02/31/26 5:00 am"),
        (CnnParser(), "CNN 5 cosas 09/17/26 13:00 am"),
        (CopeParser(), "09:30H | 31 FEB 2026 | BOLETÍN"),
        (CopeParser(), "24:00H | 17 SEP 2026 | BOLETÍN"),
        (RfiJournalMondeParser(), "Journal 31/02/2026 09h30 GMT"),
        (RfiJournalMondeParser(), "Journal 17/09/2026 24h00 GMT"),
    ],
)
def test_invalid_calendar_and_clock_values_remain_rejected(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    assert parser.parse_with_recovery(title).parsed is None


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (RneParser(), "NOTICIAS RNE - 17.09.2026"),
        (RneParser(), "NOTICIAS RNE - 09.30H"),
        (SerParser(), "Las noticias de la SER, 09:30"),
        (SerParser(), "Las noticias de la SER (17/09/2026)"),
        (OndaCeroParser(), "Las noticias de Onda Cero de las 09:30h"),
        (OndaCeroParser(), "Las noticias de Onda Cero (17/09/2026)"),
        (CnnParser(), "CNN 5 cosas 09/17/26"),
        (CnnParser(), "CNN 5 cosas 5:00 am"),
        (CopeParser(), "09:30H | BOLETÍN"),
        (CopeParser(), "17 SEP 2026 | BOLETÍN"),
        (RfiJournalMondeParser(), "Journal 17/09/2026 GMT"),
        (RfiJournalMondeParser(), "Journal 09h30 GMT"),
    ],
)
def test_missing_date_or_clock_never_recovers(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    assert parser.parse_with_recovery(title).parsed is None


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (RneParser(), "NOTICIAS RNE | 17/09/26 : 09:30 H 10:30"),
        (
            SerParser(),
            "Las noticias de la SER, 09:30 10:30 (17/09/2026)",
        ),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 09:30h 10:30 (17/09/2026)",
        ),
        (CnnParser(), "CNN 5 cosas 09/17/26 09/18/26 5:00 am"),
        (CopeParser(), "09:30H | 17 SEP 2026 | BOLETÍN | 10:30H"),
        (RfiJournalMondeParser(), "Journal 17/09/2026 09h30 10h30 GMT"),
    ],
)
def test_duplicate_or_ambiguous_temporal_tokens_never_recover(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    assert parser.parse_with_recovery(title).parsed is None


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (SerParser(), "Las noticias de la SER - 9.3017-09-26"),
        (SerParser(), "Las noticias de la SER - 9.30 (17-09-26]"),
        (SerParser(), "Las noticias de la SER - 9.30 [17-09-26)"),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 9.30H17-09-26.",
        ),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 9.30 H (17-09-26].",
        ),
    ],
)
def test_provider_compat_requires_explicit_temporal_boundary_and_balanced_brackets(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    assert parser.parse_with_recovery(title).parsed is None


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (RneParser(), "NOTICIAS\u00ad RNE - 17.09.2026 - 09.30H"),
        (SerParser(), "Las noticias de la SER\u2060, 09:30 (17/09/2026)"),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 09:30h (17∕09∕2026)",
        ),
    ],
)
def test_unreviewed_unicode_syntax_is_not_silently_recovered(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    assert parser.parse_with_recovery(title).parsed is None


@pytest.mark.parametrize(
    ("parser", "title"),
    [
        (CopeParser(), "09:30H | 17 SEP 26 | BOLETÍN"),
        (RfiJournalMondeParser(), "Journal 17/09/26 09h30 GMT"),
    ],
)
def test_four_digit_only_providers_reject_short_years(
    parser: RecoveryTitleParser,
    title: str,
) -> None:
    assert parser.parse_with_recovery(title).parsed is None


@pytest.mark.parametrize(
    ("parser", "exact_title", "recovered_title", "expected_tz"),
    [
        (
            RneParser(),
            "NOTICIAS RNE - 25.10.2026 - 02.30H",
            "NOTICIAS RNE | 25/10/26 : 02:30 H.",
            MADRID,
        ),
        (
            SerParser(),
            "Las noticias de la SER, 02:30 (25/10/2026)",
            "Las noticias de la SER - 2.30 25-10-26",
            MADRID,
        ),
        (
            OndaCeroParser(),
            "Las noticias de Onda Cero de las 02:30h (25/10/2026)",
            "Las noticias de Onda Cero de las 2.30 H 25-10-26.",
            MADRID,
        ),
        (
            CnnParser(),
            "CNN 5 cosas 11/01/26 1:30 am",
            "CNN\u00a05 cosas\u00a011/01/26\u00a01:30\u00a0am",
            NEW_YORK,
        ),
        (
            CopeParser(),
            "02:30H | 25 OCT 2026 | BOLETÍN",
            "02:30H\u200b | \u200b25 OCT 2026\u200b | \u200bBOLETÍN",
            MADRID,
        ),
        (
            RfiJournalMondeParser(),
            "Journal 25/10/2026 02h30 GMT",
            "Journal\u200b 25/10/2026\u200b 02h30\u200b GMT",
            UTC,
        ),
    ],
)
def test_recovery_rejects_ambiguous_local_time_but_preserves_gmt(
    parser: RecoveryTitleParser,
    exact_title: str,
    recovered_title: str,
    expected_tz: ZoneInfo,
) -> None:
    exact = parser.parse_with_recovery(exact_title)
    recovered = parser.parse_with_recovery(recovered_title)

    # Issue #433 supersedes the implicit fold=0 policy documented under #236.
    if expected_tz != UTC:
        assert exact.parsed is None
        assert recovered.parsed is None
        assert exact.mode == recovered.mode == "rejected"
        return

    assert exact.parsed is not None
    assert recovered.parsed is not None
    assert recovered.parsed.edition_at == exact.parsed.edition_at
    assert recovered.parsed.edition_at.tzinfo == expected_tz
