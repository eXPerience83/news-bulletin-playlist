from datetime import UTC, datetime
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

SER_URL = "https://example.test/ser.xml"
RNE_URL = "https://example.test/rne.xml"


def _source(
    source_id: str,
    parser_id: str,
    endpoint_url: str | None,
    *,
    timezone: str = "Europe/Madrid",
) -> SourceDefinition:
    return SourceDefinition(
        id=SourceId(source_id),
        display_name=source_id.upper(),
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo(timezone),
        enabled=True,
        parser_id=ParserId(parser_id),
        endpoint_url=endpoint_url,
    )


def _playlist(playlist_id: str, *source_ids: str, enabled: bool = True) -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId(playlist_id),
        display_name=playlist_id,
        description="test playlist",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=enabled,
        source_selection=SourceSelection(tuple(SourceId(value) for value in source_ids)),
        destination=DestinationReference(
            adapter_id=AdapterId("spotify"),
            external_id=f"destination-{playlist_id}",
        ),
    )


def _config(
    sources: tuple[SourceDefinition, ...], playlists: tuple[PlaylistDefinition, ...]
) -> EngineConfig:
    return EngineConfig(schema_version=1, sources=sources, playlists=playlists)


def _rss(*items: str) -> bytes:
    body = "".join(items)
    return f"<rss><channel>{body}</channel></rss>".encode()


def _item(
    *,
    guid: str | None,
    title: str,
    published: str,
    duration: str | None = None,
) -> str:
    guid_xml = "" if guid is None else f"<guid>{guid}</guid>"
    duration_xml = "" if duration is None else f"<itunes:duration>{duration}</itunes:duration>"
    namespace = ' xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"' if duration else ""
    return (
        f"<item{namespace}>{guid_xml}<title>{title}</title>"
        f"<pubDate>{published}</pubDate>{duration_xml}</item>"
    )


def test_shared_source_is_fetched_once_across_playlists() -> None:
    ser = _source("ser", "ser", SER_URL)
    rne = _source("rne", "rne", RNE_URL)
    config = _config(
        (ser, rne),
        (
            _playlist("first", "ser"),
            _playlist("second", "ser", "rne"),
        ),
    )
    payloads = {
        SER_URL: _rss(
            _item(
                guid="ser-1",
                title="Las noticias de la SER, 11:00 (30/08/2026)",
                published="Sun, 30 Aug 2026 09:05:00 +0000",
            )
        ),
        RNE_URL: _rss(
            _item(
                guid="rne-1",
                title="NOTICIAS RNE - 30.08.2026 - 11.00 H",
                published="Sun, 30 Aug 2026 09:06:00 +0000",
            )
        ),
    }
    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return payloads[url]

    result = collect_required_sources(config, fetcher=fetcher)

    assert calls == [SER_URL, RNE_URL]
    assert all(source.ok for source in result.sources)
    assert len(result.editions) == 2


def test_disabled_playlist_does_not_add_collection_work() -> None:
    ser = _source("ser", "ser", SER_URL)
    rne = _source("rne", "rne", RNE_URL)
    config = _config(
        (ser, rne),
        (
            _playlist("enabled", "ser"),
            _playlist("disabled", "rne", enabled=False),
        ),
    )
    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return _rss(
            _item(
                guid="ser-1",
                title="Las noticias de la SER, 11:00 (30/08/2026)",
                published="Sun, 30 Aug 2026 09:05:00 +0000",
            )
        )

    result = collect_required_sources(config, fetcher=fetcher)

    assert calls == [SER_URL]
    assert [source.source_id for source in result.sources] == [SourceId("ser")]


def test_same_title_and_time_with_distinct_native_ids_are_preserved() -> None:
    ser = _source("ser", "ser", SER_URL)
    config = _config((ser,), (_playlist("first", "ser"),))
    title = "Las noticias de la SER, 11:00 (30/08/2026)"
    payload = _rss(
        _item(
            guid="asset-a",
            title=title,
            published="Sun, 30 Aug 2026 09:05:00 +0000",
        ),
        _item(
            guid="asset-b",
            title=title,
            published="Sun, 30 Aug 2026 09:05:00 +0000",
        ),
    )

    result = collect_required_sources(config, fetcher=lambda _url: payload)

    identities = [edition.identity for edition in result.editions]
    assert identities == [
        (SourceId("ser"), "asset-a"),
        (SourceId("ser"), "asset-b"),
    ]


def test_duplicate_native_id_is_not_emitted_twice() -> None:
    ser = _source("ser", "ser", SER_URL)
    config = _config((ser,), (_playlist("first", "ser"),))
    item = _item(
        guid="asset-a",
        title="Las noticias de la SER, 11:00 (30/08/2026)",
        published="Sun, 30 Aug 2026 09:05:00 +0000",
    )

    result = collect_required_sources(config, fetcher=lambda _url: _rss(item, item))

    assert len(result.editions) == 1
    assert result.editions[0].source_native_id == "asset-a"


def test_published_at_and_edition_at_remain_distinct_and_duration_is_preserved() -> None:
    ser = _source("ser", "ser", SER_URL)
    config = _config((ser,), (_playlist("first", "ser"),))
    payload = _rss(
        _item(
            guid="asset-a",
            title="Las noticias de la SER, 11:00 (30/08/2026)",
            published="Sun, 30 Aug 2026 09:05:00 +0000",
            duration="01:02:03",
        )
    )

    result = collect_required_sources(config, fetcher=lambda _url: payload)
    edition = result.editions[0]

    assert edition.published_at == datetime(2026, 8, 30, 9, 5, tzinfo=UTC)
    assert edition.edition_at == datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    assert edition.duration_seconds == 3723


def test_source_timezone_is_authoritative_for_parsed_edition_time() -> None:
    ser = _source("ser", "ser", SER_URL, timezone="UTC")
    config = _config((ser,), (_playlist("first", "ser"),))
    payload = _rss(
        _item(
            guid="asset-a",
            title="Las noticias de la SER, 11:00 (30/08/2026)",
            published="Sun, 30 Aug 2026 11:05:00 +0000",
        )
    )

    result = collect_required_sources(config, fetcher=lambda _url: payload)

    assert result.editions[0].edition_at == datetime(2026, 8, 30, 11, 0, tzinfo=UTC)


def test_one_source_failure_does_not_abort_unrelated_sources() -> None:
    ser = _source("ser", "ser", SER_URL)
    rne = _source("rne", "rne", RNE_URL)
    config = _config((ser, rne), (_playlist("first", "ser", "rne"),))
    rne_payload = _rss(
        _item(
            guid="rne-1",
            title="NOTICIAS RNE - 30.08.2026 - 11.00 H",
            published="Sun, 30 Aug 2026 09:06:00 +0000",
        )
    )

    def fetcher(url: str) -> bytes:
        if url == SER_URL:
            raise OSError("SER unavailable")
        return rne_payload

    result = collect_required_sources(config, fetcher=fetcher)
    by_source = {source.source_id: source for source in result.sources}

    assert not by_source[SourceId("ser")].ok
    assert "SER unavailable" in (by_source[SourceId("ser")].error or "")
    assert by_source[SourceId("rne")].ok
    assert len(by_source[SourceId("rne")].editions) == 1


def test_malformed_feed_is_failure_not_empty_success() -> None:
    ser = _source("ser", "ser", SER_URL)
    config = _config((ser,), (_playlist("first", "ser"),))

    result = collect_required_sources(config, fetcher=lambda _url: b"<rss><channel>")

    assert not result.sources[0].ok
    assert result.sources[0].editions == ()


def test_unrecognized_or_identity_less_feed_is_failure_not_empty_success() -> None:
    ser = _source("ser", "ser", SER_URL)
    config = _config((ser,), (_playlist("first", "ser"),))
    payload = _rss(
        _item(
            guid=None,
            title="Las noticias de la SER, 11:00 (30/08/2026)",
            published="Sun, 30 Aug 2026 09:05:00 +0000",
        ),
        _item(
            guid="other-1",
            title="Hoy por Hoy: noticias 11:00",
            published="Sun, 30 Aug 2026 09:06:00 +0000",
        ),
    )

    result = collect_required_sources(config, fetcher=lambda _url: payload)

    assert not result.sources[0].ok
    assert result.sources[0].editions == ()
    assert result.sources[0].error == "feed contained no canonical bulletin editions"


def test_required_source_without_endpoint_is_reported_without_fetching() -> None:
    ser = _source("ser", "ser", None)
    config = _config((ser,), (_playlist("first", "ser"),))
    called = False

    def fetcher(_url: str) -> bytes:
        nonlocal called
        called = True
        raise AssertionError("fetcher should not be called")

    result = collect_required_sources(config, fetcher=fetcher)

    assert not called
    assert not result.sources[0].ok
    assert result.sources[0].error == "required source has no endpoint_url"
