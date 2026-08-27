from news_bulletin_playlist.provider_watch import evaluate_titles, extract_rss_titles
from news_bulletin_playlist.providers.ser import SerParser


def test_extract_rss_titles() -> None:
    payload = b"""<?xml version='1.0' encoding='UTF-8'?>
    <rss><channel>
      <item><title>First</title></item>
      <item><title>Second</title></item>
    </channel></rss>"""
    assert extract_rss_titles(payload) == ["First", "Second"]


def test_extract_rss_titles_handles_namespaced_rss() -> None:
    payload = b"""<rss xmlns:x='urn:test'><channel>
      <item><x:meta>ignored</x:meta><title>News</title></item>
    </channel></rss>"""
    assert extract_rss_titles(payload) == ["News"]


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
