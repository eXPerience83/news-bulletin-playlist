from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

import news_bulletin_playlist.collection as collection_module
from news_bulletin_playlist.collection import collect_required_sources
from news_bulletin_playlist.models import (
    AdapterId,
    CountryCode,
    DestinationReference,
    EngineConfig,
    LanguageTag,
    ParserId,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.provider_watch import evaluate_titles, extract_fallback_titles
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
def test_recovery_preserves_provider_timezone_and_ambiguous_wall_clock_policy(
    parser: RecoveryTitleParser,
    exact_title: str,
    recovered_title: str,
    expected_tz: ZoneInfo,
) -> None:
    exact = parser.parse_with_recovery(exact_title)
    recovered = parser.parse_with_recovery(recovered_title)

    assert exact.parsed is not None
    assert recovered.parsed is not None
    assert recovered.parsed.edition_at == exact.parsed.edition_at
    assert recovered.parsed.edition_at.fold == exact.parsed.edition_at.fold
    assert recovered.parsed.edition_at.tzinfo == expected_tz


def test_ondacero_fallback_prefilter_accepts_safe_whitespace_normalization() -> None:
    title = "Las\u00a0noticias de Onda Cero de las 10:00h (17/09/2026)"
    payload = f"<html><body><h3>{title}</h3></body></html>".encode()

    titles = extract_fallback_titles("ondacero", payload)
    result = evaluate_titles("ondacero", OndaCeroParser(), titles)

    assert titles == [title]
    assert result.ok is True
    assert result.parsed == 1
    assert "recovered 1/1" in result.detail


def test_title_parse_denominator_excludes_preparse_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Filter:
        def accepts(self, title: str) -> bool:
            return title != "filter me"

    monkeypatch.setattr(
        collection_module,
        "get_collection_filter",
        lambda _filter_id: _Filter(),
    )

    source = SourceDefinition(
        id=SourceId("ser-test"),
        display_name="SER test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=MADRID,
        enabled=True,
        parser_id=ParserId("ser"),
        collection_filter_id="test_filter",
        endpoint_url="https://example.test/news.xml",
    )
    playlist = PlaylistDefinition(
        id=PlaylistId("playlist"),
        display_name="Playlist",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((source.id,)),
        destination=DestinationReference(AdapterId("spotify"), "destination"),
    )
    config = EngineConfig(schema_version=1, sources=(source,), playlists=(playlist,))

    valid = "Las noticias de la SER, 09:30 (17/09/2026)"
    recovered = "Las noticias de la SER - 8.30 17-09-26"
    published = "Thu, 17 Sep 2026 07:35:00 +0000"

    def item(guid: str, title: str | None, publication: str = published) -> str:
        title_xml = "" if title is None else f"<title>{title}</title>"
        return (
            "<item>"
            f"<guid>{guid}</guid>"
            f"{title_xml}"
            f"<pubDate>{publication}</pubDate>"
            "</item>"
        )

    payload = (
        "<rss><channel>"
        + item("g1", valid)
        + item("g2", None)
        + item("g3", "filter me")
        + item("g1", "SER DEPORTES - 09:30 (17/09/2026)")
        + item("g4", valid, "not-a-date")
        + item("g5", recovered, "Thu, 17 Sep 2026 06:35:00 +0000")
        + "</channel></rss>"
    ).encode()

    result = collect_required_sources(config, fetcher=lambda _url: payload)
    collected = result.sources[0]
    diagnostics = collected.collection_diagnostics

    assert collected.ok is True
    assert len(collected.editions) == 2
    assert diagnostics.rss_item_count == 6
    assert diagnostics.rss_missing_required_fields_count == 1
    assert diagnostics.rss_filter_rejected_count == 1
    assert diagnostics.rss_duplicate_native_id_count == 1
    assert diagnostics.rss_invalid_published_at_count == 1
    assert diagnostics.rss_title_parser_attempted_count == 2
    assert diagnostics.rss_title_parser_exact_count == 1
    assert diagnostics.rss_title_parser_provider_compat_recovered_count == 1
    assert diagnostics.rss_title_parser_rejected_count == 0
