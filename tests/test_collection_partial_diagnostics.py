from __future__ import annotations

from test_collection import RNE_URL, SER_URL, _config, _playlist, _raw_item, _rss, _source

import news_bulletin_playlist.collection as collection_module
from news_bulletin_playlist.collection import collect_required_sources
from news_bulletin_playlist.models import SourceId


def test_normalization_error_preserves_safely_known_partial_collection_counters(
    monkeypatch,
) -> None:
    class _ExplodingFilter:
        def accepts(self, title: str) -> bool:
            if title == "explode now":
                raise ValueError("synthetic filter failure")
            return title != "reject by filter"

    monkeypatch.setattr(
        collection_module,
        "get_collection_filter",
        lambda _filter_id: _ExplodingFilter(),
    )
    source = _source(
        "release",
        "release_date_title",
        SER_URL,
        collection_filter_id="test_filter",
    )
    config = _config((source,), (_playlist("first", "release"),))
    published = "Sun, 30 Aug 2026 09:05:00 +0000"
    payload = _rss(
        _raw_item(
            guid="missing-title",
            title=None,
            published=published,
        ),
        _raw_item(
            guid="filtered",
            title="reject by filter",
            published=published,
        ),
        _raw_item(
            guid="bad-date",
            title="accepted by filter",
            published="definitely-not-a-date",
        ),
        _raw_item(
            enclosure="https://example.test/exploding.mp3",
            title="explode now",
            published=published,
        ),
        _raw_item(
            link="https://example.test/not-processed",
            title="not processed",
            published=published,
        ),
    )

    result = collect_required_sources(config, fetcher=lambda _url: payload)
    source_result = result.sources[0]
    diagnostics = source_result.collection_diagnostics

    assert not source_result.ok
    assert source_result.editions == ()
    assert source_result.error == "synthetic filter failure"
    assert diagnostics.failure_reason == "normalization_error"
    assert diagnostics.rss_item_count == 5
    assert diagnostics.rss_missing_required_fields_count == 1
    assert diagnostics.rss_filter_rejected_count == 1
    assert diagnostics.rss_duplicate_native_id_count == 0
    assert diagnostics.rss_invalid_published_at_count == 1
    assert diagnostics.rss_title_parser_rejected_count is None
    assert diagnostics.rss_native_id_guid_count == 3
    assert diagnostics.rss_native_id_enclosure_count == 1
    assert diagnostics.rss_native_id_link_count == 0


def test_normalization_oserror_isolated_to_source_and_preserves_partial_counters(
    monkeypatch,
) -> None:
    class _ExplodingFilter:
        def accepts(self, title: str) -> bool:
            if title == "explode now":
                raise OSError("synthetic normalization io failure")
            return True

    monkeypatch.setattr(
        collection_module,
        "get_collection_filter",
        lambda _filter_id: _ExplodingFilter(),
    )
    failing = _source(
        "release",
        "release_date_title",
        SER_URL,
        collection_filter_id="test_filter",
    )
    healthy = _source("rne", "rne", RNE_URL)
    config = _config((failing, healthy), (_playlist("first", "release", "rne"),))
    published = "Sun, 30 Aug 2026 09:05:00 +0000"
    payloads = {
        SER_URL: _rss(
            _raw_item(guid="missing-title", title=None, published=published),
            _raw_item(guid="exploding", title="explode now", published=published),
            _raw_item(guid="not-processed", title="not processed", published=published),
        ),
        RNE_URL: _rss(
            _raw_item(
                guid="rne-1",
                title="NOTICIAS RNE - 30.08.2026 - 11.00 H",
                published="Sun, 30 Aug 2026 09:06:00 +0000",
            )
        ),
    }

    result = collect_required_sources(config, fetcher=lambda url: payloads[url])
    by_source = {source.source_id: source for source in result.sources}
    failed = by_source[SourceId("release")]
    diagnostics = failed.collection_diagnostics

    assert not failed.ok
    assert failed.error == "synthetic normalization io failure"
    assert diagnostics.failure_reason == "normalization_error"
    assert diagnostics.rss_item_count == 3
    assert diagnostics.rss_missing_required_fields_count == 1
    assert diagnostics.rss_native_id_guid_count == 2
    assert by_source[SourceId("rne")].ok
    assert len(by_source[SourceId("rne")].editions) == 1
