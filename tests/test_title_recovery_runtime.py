from __future__ import annotations

from zoneinfo import ZoneInfo

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

URL = "https://example.test/news.xml"


def _source(parser_id: str = "ser") -> SourceDefinition:
    return SourceDefinition(
        id=SourceId("source"),
        display_name="Source",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId(parser_id),
        endpoint_url=URL,
    )


def _config(parser_id: str = "ser") -> EngineConfig:
    source = _source(parser_id)
    playlist = PlaylistDefinition(
        id=PlaylistId("playlist"),
        display_name="Playlist",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((source.id,)),
        destination=DestinationReference(
            adapter_id=AdapterId("spotify"),
            external_id="destination",
        ),
    )
    return EngineConfig(schema_version=1, sources=(source,), playlists=(playlist,))


def _rss(*titles: str) -> bytes:
    items = []
    for index, title in enumerate(titles):
        items.append(
            "<item>"
            f"<guid>item-{index}</guid>"
            f"<title>{title}</title>"
            "<pubDate>Thu, 17 Sep 2026 07:35:00 +0000</pubDate>"
            "</item>"
        )
    return f"<rss><channel>{''.join(items)}</channel></rss>".encode()


def test_runtime_counts_exact_normalized_and_provider_compat_from_one_parse_attempt() -> None:
    exact = "Las noticias de la SER, 09:30 (17/09/2026)"
    normalized = "Las noticias\u00a0de la SER, 09:30 (17/09/2026)"
    provider_compat = "Las noticias de la SER - 9.30 17-09-26"

    result = collect_required_sources(
        _config(),
        fetcher=lambda _url: _rss(exact, normalized, provider_compat),
    )
    source = result.sources[0]
    diagnostics = source.collection_diagnostics

    assert source.ok is True
    assert len(source.editions) == 3
    assert [edition.title for edition in source.editions] == [exact, normalized, provider_compat]
    assert diagnostics.rss_title_parser_attempted_count == 3
    assert diagnostics.rss_title_parser_exact_count == 1
    assert diagnostics.rss_title_parser_recovered_count == 2
    assert diagnostics.rss_title_parser_normalized_recovered_count == 1
    assert diagnostics.rss_title_parser_provider_compat_recovered_count == 1
    assert diagnostics.rss_title_parser_rejected_count == 0


def test_runtime_marks_material_unresolved_title_drift_as_collection_failure() -> None:
    valid = "Las noticias de la SER, 09:30 (17/09/2026)"
    rejected_one = "SER DEPORTES - 09:30 (17/09/2026)"
    rejected_two = "new unreviewed title shape"

    result = collect_required_sources(
        _config(),
        fetcher=lambda _url: _rss(valid, rejected_one, rejected_two),
    )
    source = result.sources[0]
    diagnostics = source.collection_diagnostics

    assert source.ok is False
    assert source.editions == ()
    assert source.error == "title parser contract degraded"
    assert diagnostics.failure_reason == "title_parser_drift"
    assert diagnostics.rss_title_parser_attempted_count == 3
    assert diagnostics.rss_title_parser_exact_count == 1
    assert diagnostics.rss_title_parser_recovered_count == 0
    assert diagnostics.rss_title_parser_rejected_count == 2


def test_runtime_drift_threshold_is_strictly_below_half() -> None:
    valid_a = "Las noticias de la SER, 09:30 (17/09/2026)"
    valid_b = "Las noticias de la SER, 08:30 (17/09/2026)"
    rejected_a = "SER DEPORTES - 09:30 (17/09/2026)"
    rejected_b = "another unreviewed title shape"

    result = collect_required_sources(
        _config(),
        fetcher=lambda _url: _rss(valid_a, valid_b, rejected_a, rejected_b),
    )
    source = result.sources[0]

    assert source.ok is True
    assert len(source.editions) == 2
    assert source.collection_diagnostics.failure_reason is None
    assert source.collection_diagnostics.rss_title_parser_attempted_count == 4
    assert source.collection_diagnostics.rss_title_parser_rejected_count == 2


def test_rejected_duplicate_native_id_does_not_inflate_drift_denominator() -> None:
    valid = "Las noticias de la SER, 09:30 (17/09/2026)"
    published = "Thu, 17 Sep 2026 07:35:00 +0000"
    payload = (
        "<rss><channel>"
        f"<item><guid>good</guid><title>{valid}</title><pubDate>{published}</pubDate></item>"
        f"<item><guid>bad</guid><title>PROMO</title><pubDate>{published}</pubDate></item>"
        f"<item><guid>bad</guid><title>PROMO</title><pubDate>{published}</pubDate></item>"
        "</channel></rss>"
    ).encode()

    result = collect_required_sources(_config(), fetcher=lambda _url: payload)
    source = result.sources[0]
    diagnostics = source.collection_diagnostics

    assert source.ok is True
    assert len(source.editions) == 1
    assert diagnostics.failure_reason is None
    assert diagnostics.rss_duplicate_native_id_count == 1
    assert diagnostics.rss_title_parser_attempted_count == 2
    assert diagnostics.rss_title_parser_exact_count == 1
    assert diagnostics.rss_title_parser_rejected_count == 1


def test_first_parse_eligible_duplicate_native_id_is_authoritative_for_cycle() -> None:
    valid = "Las noticias de la SER, 09:30 (17/09/2026)"
    published = "Thu, 17 Sep 2026 07:35:00 +0000"
    payload = (
        "<rss><channel>"
        f"<item><guid>shared</guid><title>PROMO</title><pubDate>{published}</pubDate></item>"
        f"<item><guid>shared</guid><title>{valid}</title><pubDate>{published}</pubDate></item>"
        f"<item><guid>good</guid><title>{valid}</title><pubDate>{published}</pubDate></item>"
        "</channel></rss>"
    ).encode()

    result = collect_required_sources(_config(), fetcher=lambda _url: payload)
    source = result.sources[0]
    diagnostics = source.collection_diagnostics

    assert source.ok is True
    assert [edition.source_native_id for edition in source.editions] == ["good"]
    assert diagnostics.rss_duplicate_native_id_count == 1
    assert diagnostics.rss_title_parser_attempted_count == 2
    assert diagnostics.rss_title_parser_exact_count == 1
    assert diagnostics.rss_title_parser_rejected_count == 1


def test_release_date_title_strategy_is_not_subject_to_semantic_parser_drift() -> None:
    result = collect_required_sources(
        _config("release_date_title"),
        fetcher=lambda _url: _rss("anything", "another title", "third title"),
    )
    source = result.sources[0]
    diagnostics = source.collection_diagnostics

    assert source.ok is True
    assert len(source.editions) == 3
    assert all(edition.edition_at is None for edition in source.editions)
    assert diagnostics.rss_title_parser_attempted_count is None
    assert diagnostics.rss_title_parser_exact_count is None
    assert diagnostics.rss_title_parser_recovered_count is None
    assert diagnostics.rss_title_parser_rejected_count is None


def test_small_all_rejected_sample_keeps_existing_no_canonical_failure_class() -> None:
    result = collect_required_sources(
        _config(),
        fetcher=lambda _url: _rss("unreviewed one", "unreviewed two"),
    )
    source = result.sources[0]

    assert source.ok is False
    assert source.editions == ()
    assert source.collection_diagnostics.failure_reason == "no_canonical_editions"
