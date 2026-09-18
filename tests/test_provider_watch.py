import urllib.error
from zoneinfo import ZoneInfo

import pytest

import news_bulletin_playlist.provider_watch as provider_watch_module
from news_bulletin_playlist.models import (
    CountryCode,
    LanguageTag,
    ParserId,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.provider_watch import (
    check_catalog_source,
    check_provider,
    evaluate_titles,
    extract_fallback_titles,
    extract_rss_titles,
    format_report,
)
from news_bulletin_playlist.providers.ser import SerParser
from news_bulletin_playlist.registry import ProviderConfig


def test_extract_rss_titles() -> None:
    payload = b"""<?xml version='1.0' encoding='UTF-8'?>
    <rss><channel>
      <item><title>First</title></item>
      <item><title>Second</title></item>
    </channel></rss>"""
    assert extract_rss_titles(payload) == ["First", "Second"]


def test_extract_rss_titles_keeps_nested_suffix_text() -> None:
    payload = b"""<rss><channel><item>
      <title>Las noticias de la SER, 09:30 (17/09/2026)<span> - DEPORTES</span></title>
    </item></channel></rss>"""

    assert extract_rss_titles(payload) == ["Las noticias de la SER, 09:30 (17/09/2026) - DEPORTES"]


def test_extract_rss_titles_handles_namespaced_rss() -> None:
    payload = b"""<rss xmlns:x='urn:test'><channel>
      <item><x:meta>ignored</x:meta><title>News</title></item>
    </channel></rss>"""
    assert extract_rss_titles(payload) == ["News"]


def test_extract_ondacero_titles_from_official_html_fallback() -> None:
    payload = b"""<html><body>
      <h3>Las noticias de Onda Cero de las 10:00h (27/8/2026)</h3>
      <script>"Las noticias de Onda Cero de las 10:00h (27/8/2026)"</script>
      <h3>Las noticias de Onda Cero de las 9:00h (27/8/2026)</h3>
    </body></html>"""
    assert extract_fallback_titles("ondacero", payload) == [
        "Las noticias de Onda Cero de las 10:00h (27/8/2026)",
        "Las noticias de Onda Cero de las 9:00h (27/8/2026)",
    ]


@pytest.mark.parametrize("container", ["div", "article"])
def test_ondacero_fallback_does_not_promote_inline_fragment_from_broader_context(
    container: str,
) -> None:
    title = "Las noticias de Onda Cero de las 09:30h (17/09/2026)"
    payload = (
        f"<html><body><{container}>Promo: <span>{title}</span> - DEPORTES"
        f"</{container}></body></html>"
    ).encode()

    assert extract_fallback_titles("ondacero", payload) == []


def test_ondacero_fallback_does_not_promote_nested_context_fragment() -> None:
    title = "Las noticias de Onda Cero de las 09:30h (17/09/2026)"
    payload = (
        "<html><body><section>Promo: "
        f"<div><span>{title}</span></div>"
        " - DEPORTES</section></body></html>"
    ).encode()

    assert extract_fallback_titles("ondacero", payload) == []


def test_unknown_provider_has_no_html_fallback_extractor() -> None:
    assert extract_fallback_titles("unknown", b"<html>News</html>") == []


def test_contract_accepts_majority_of_recent_titles() -> None:
    titles = [
        "Las noticias de la SER, 10:03 (27/08/2026)",
        "Las noticias de la SER, 09:02 (27/08/2026)",
        "special episode",
    ]
    result = evaluate_titles("ser", SerParser(), titles)
    assert result.ok
    assert result.parsed == 2


def test_contract_rejects_broken_recent_title_shape() -> None:
    titles = ["new format one", "new format two", "Las noticias de la SER, 08:00 (27/08/2026)"]
    result = evaluate_titles("ser", SerParser(), titles)
    assert not result.ok
    assert result.parsed == 1


def test_contract_rejects_empty_feed() -> None:
    result = evaluate_titles("ser", SerParser(), [])
    assert not result.ok


def test_provider_watch_sanitizes_provider_controlled_http_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "UNTRUSTED_PROVIDER_MARKER https://example.test/private-title"

    def fail_fetch(_url: str, timeout: float = 20.0) -> bytes:
        del timeout
        raise urllib.error.HTTPError(
            "https://example.test/private-title",
            403,
            marker,
            None,
            None,
        )

    monkeypatch.setattr(provider_watch_module, "fetch_feed", fail_fetch)
    provider = ProviderConfig(
        provider_id="ser",
        feed_url="https://example.test/feed",
        spotify_show_id="show",
        parser=SerParser(),
    )

    result = check_provider(provider)
    report = format_report([result])

    assert result.ok is False
    assert "HTTP 403" in report
    assert marker not in report
    assert "private-title" not in report


def test_provider_watch_sanitizes_both_primary_and_fallback_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "UPSTREAM_PRIVATE_TEXT"

    def fail_fetch(url: str, timeout: float = 20.0) -> bytes:
        del timeout
        code = 503 if url.endswith("/feed") else 403
        raise urllib.error.HTTPError(url, code, marker, None, None)

    monkeypatch.setattr(provider_watch_module, "fetch_feed", fail_fetch)
    provider = ProviderConfig(
        provider_id="ondacero",
        feed_url="https://example.test/feed",
        spotify_show_id="show",
        parser=SerParser(),
        contract_fallback_url="https://example.test/fallback",
    )

    result = check_provider(provider)
    report = format_report([result])

    assert result.ok is False
    assert "HTTP 503" in report
    assert "HTTP 403" in report
    assert marker not in report
    assert "example.test" not in report


def test_catalog_watch_sanitizes_runtime_normalization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "PRIVATE_PROVIDER_PAYLOAD https://example.test/secret"
    source = SourceDefinition(
        id=SourceId("ser-test"),
        display_name="SER",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ser"),
        endpoint_url="https://example.test/feed",
    )

    monkeypatch.setattr(provider_watch_module, "fetch_feed", lambda _url: b"<rss/>")

    def fail_normalize(_source: SourceDefinition, _payload: bytes):
        raise ValueError(marker)

    monkeypatch.setattr(provider_watch_module, "normalize_rss_source", fail_normalize)

    result = check_catalog_source(source)
    report = format_report([result])

    assert result.ok is False
    assert "invalid provider payload" in report
    assert marker not in report
    assert "secret" not in report
