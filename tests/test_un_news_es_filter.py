from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.collection import normalize_rss_source
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.spotify.matcher import match_source_editions


def _payload(*items: tuple[str, str]) -> bytes:
    body = "".join(
        f"<item><guid>{index}</guid><title>{title}</title><pubDate>{published}</pubDate></item>"
        for index, (title, published) in enumerate(items)
    )
    return f"<rss><channel>{body}</channel></rss>".encode()


def test_un_news_es_filter_accepts_only_dated_product_editions() -> None:
    source = BUILTIN_CATALOG.source("un_news_es")
    editions = normalize_rss_source(
        source,
        _payload(
            ("La ONU en Minutos 28 de mayo de 2026", "Thu, 28 May 2026 12:00:00 -0400"),
            ("La ONU en Minutos 23 abril de 2026", "Thu, 23 Apr 2026 12:00:00 -0400"),
            (
                "La ONU reivindica el español como lengua de poder",
                "Thu, 28 May 2026 12:00:00 -0400",
            ),
            (
                "La ONU en Minutos no se publicará hasta el 31 de Agosto",
                "Thu, 28 May 2026 12:00:00 -0400",
            ),
            ("La ONU en Minutos 31 de febrero de 2026", "Thu, 28 May 2026 12:00:00 -0400"),
        ),
    )
    assert [edition.title for edition in editions] == [
        "La ONU en Minutos 28 de mayo de 2026",
        "La ONU en Minutos 23 abril de 2026",
    ]
    assert all(edition.edition_at is None for edition in editions)


def test_un_news_es_all_unrelated_feed_fails_closed() -> None:
    source = BUILTIN_CATALOG.source("un_news_es")
    with pytest.raises(ValueError, match="no canonical bulletin editions"):
        normalize_rss_source(
            source,
            _payload(("Otra producción de Noticias ONU", "Thu, 28 May 2026 12:00:00 -0400")),
        )


def test_un_news_es_uses_existing_release_date_title_matching(tmp_path: Path) -> None:
    source = BUILTIN_CATALOG.source("un_news_es")
    edition = normalize_rss_source(
        source,
        _payload(("La ONU en Minutos 28 de mayo de 2026", "Thu, 28 May 2026 12:00:00 -0400")),
    )[0]
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.upsert_editions((edition,), observed_at=datetime(2026, 5, 28, 18, tzinfo=UTC))

    class Client:
        def show_episodes(
            self, show_id: str, *, limit: int = 50, offset: int = 0
        ) -> dict[str, Any]:
            assert show_id == "77hGWK2o0NYsdS8WuXiLo6"
            return {
                "items": [
                    {
                        "uri": "spotify:episode:onu",
                        "name": edition.title,
                        "release_date": "2026-05-28",
                        "release_date_precision": "day",
                        "duration_ms": 400000,
                    }
                ],
                "next": None,
            }

    result = match_source_editions(
        Client(), store, source, (edition,), now=datetime(2026, 5, 28, 18, tzinfo=UTC)
    )
    assert result.outcomes[0].status is MatchStatus.MATCHED
