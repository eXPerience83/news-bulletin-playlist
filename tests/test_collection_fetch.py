import gzip
from types import TracebackType
from typing import Self

import pytest

from news_bulletin_playlist import collection


class _Headers(dict[str, str]):
    pass


class _Response:
    def __init__(self, payload: bytes, *, content_encoding: str | None = None) -> None:
        self.payload = payload
        self.requested_bytes: int | None = None
        self.headers = _Headers()
        if content_encoding is not None:
            self.headers["Content-Encoding"] = content_encoding

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.requested_bytes = size
        return self.payload[:size]


class _OversizedResponse(_Response):
    def __init__(self) -> None:
        super().__init__(b"")

    def read(self, size: int = -1) -> bytes:
        self.requested_bytes = size
        return b"x" * size


def test_fetch_feed_rejects_payload_over_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _OversizedResponse()
    monkeypatch.setattr(collection.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="exceeds 10 MiB limit"):
        collection.fetch_feed("https://example.test/feed.xml")

    assert response.requested_bytes == collection._MAX_FEED_BYTES + 1


def test_fetch_feed_decodes_gzip_content_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = b"<rss><channel><item><title>News</title></item></channel></rss>"
    response = _Response(gzip.compress(xml), content_encoding="gzip")
    monkeypatch.setattr(collection.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    assert collection.fetch_feed("https://example.test/feed.xml") == xml


def test_fetch_feed_decodes_gzip_magic_without_header(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = b"<rss><channel><item><title>News</title></item></channel></rss>"
    response = _Response(gzip.compress(xml))
    monkeypatch.setattr(collection.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    assert collection.fetch_feed("https://example.test/feed.xml") == xml


def test_fetch_feed_rejects_gzip_expansion_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = gzip.compress(b"x" * (collection._MAX_FEED_BYTES + 1))
    response = _Response(payload, content_encoding="gzip")
    monkeypatch.setattr(collection.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="decoded feed payload exceeds 10 MiB limit"):
        collection.fetch_feed("https://example.test/feed.xml")
