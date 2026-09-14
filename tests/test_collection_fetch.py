import gzip
import socket
import ssl
import urllib.error
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


class _Opener:
    def __init__(
        self,
        response: _Response | None = None,
        *,
        error: BaseException | None = None,
        redirect_handler: collection._RedirectCounter | None = None,
        redirects: int = 0,
    ) -> None:
        self.response = response
        self.error = error
        self.redirect_handler = redirect_handler
        self.redirects = redirects
        self.calls = 0

    def open(self, *_args: object, **_kwargs: object) -> _Response:
        self.calls += 1
        if self.redirect_handler is not None:
            self.redirect_handler.redirect_count += self.redirects
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _install_opener(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: _Response | None = None,
    error: BaseException | None = None,
    redirects: int = 0,
) -> list[_Opener]:
    created: list[_Opener] = []

    def build_opener(*handlers: object) -> _Opener:
        redirect_handler = next(
            (
                handler
                for handler in handlers
                if isinstance(handler, collection._RedirectCounter)
            ),
            None,
        )
        opener = _Opener(
            response,
            error=error,
            redirect_handler=redirect_handler,
            redirects=redirects,
        )
        created.append(opener)
        return opener

    monkeypatch.setattr(collection.urllib.request, "build_opener", build_opener)
    return created


def _last_created(created: list[_Opener]) -> _Opener:
    assert created
    return created[-1]


def test_fetch_feed_rejects_payload_over_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _OversizedResponse()
    created = _install_opener(monkeypatch, response=response)

    with pytest.raises(ValueError, match="exceeds 10 MiB limit"):
        collection.fetch_feed("https://example.test/feed.xml")

    assert response.requested_bytes == collection._MAX_FEED_BYTES + 1
    assert _last_created(created).calls == 1


def test_fetch_feed_decodes_gzip_content_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = b"<rss><channel><item><title>News</title></item></channel></rss>"
    response = _Response(gzip.compress(xml), content_encoding="gzip")
    created = _install_opener(monkeypatch, response=response)

    result = collection.fetch_feed("https://example.test/feed.xml")

    assert result == xml
    assert isinstance(result, bytes)
    assert _last_created(created).calls == 1


def test_fetch_feed_decodes_gzip_magic_without_header(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = b"<rss><channel><item><title>News</title></item></channel></rss>"
    response = _Response(gzip.compress(xml))
    created = _install_opener(monkeypatch, response=response)

    assert collection.fetch_feed("https://example.test/feed.xml") == xml
    assert _last_created(created).calls == 1


def test_fetch_feed_rejects_gzip_expansion_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = gzip.compress(b"x" * (collection._MAX_FEED_BYTES + 1))
    response = _Response(payload, content_encoding="gzip")
    created = _install_opener(monkeypatch, response=response)

    with pytest.raises(ValueError, match="decoded feed payload exceeds 10 MiB limit"):
        collection.fetch_feed("https://example.test/feed.xml")

    assert _last_created(created).calls == 1


def test_fetch_feed_observes_success_duration_and_redirect_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(b"<rss />")
    created = _install_opener(monkeypatch, response=response, redirects=2)
    ticks = iter((10.0, 10.125))
    monkeypatch.setattr(collection.time, "monotonic", lambda: next(ticks))

    result = collection.fetch_feed("https://example.test/feed.xml")

    assert result == b"<rss />"
    assert isinstance(result, collection._ObservedFeedPayload)
    assert result.rss_fetch_duration_ms == 125
    assert result.rss_redirect_count == 2
    assert _last_created(created).calls == 1


@pytest.mark.parametrize(
    ("error", "expected_kind"),
    [
        (TimeoutError("provider-secret"), "timeout"),
        (urllib.error.URLError(socket.gaierror("provider-secret")), "dns"),
        (urllib.error.URLError(ssl.SSLError("provider-secret")), "tls"),
        (urllib.error.URLError(ConnectionResetError("provider-secret")), "connection"),
        (
            urllib.error.HTTPError(
                "https://provider-secret.invalid/feed",
                404,
                "provider-secret",
                hdrs=None,
                fp=None,
            ),
            "http_4xx",
        ),
        (
            urllib.error.HTTPError(
                "https://provider-secret.invalid/feed",
                503,
                "provider-secret",
                hdrs=None,
                fp=None,
            ),
            "http_5xx",
        ),
        (
            urllib.error.HTTPError(
                "https://provider-secret.invalid/feed",
                310,
                "provider-secret",
                hdrs=None,
                fp=None,
            ),
            "redirect_error",
        ),
    ],
)
def test_fetch_feed_preserves_exception_and_attaches_allowlisted_failure_observation(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected_kind: str,
) -> None:
    created = _install_opener(monkeypatch, error=error, redirects=1)
    ticks = iter((20.0, 20.250))
    monkeypatch.setattr(collection.time, "monotonic", lambda: next(ticks))

    with pytest.raises(type(error)) as raised:
        collection.fetch_feed("https://example.test/feed.xml")

    assert raised.value is error
    assert getattr(error, collection._FETCH_DURATION_ATTR) == 250
    assert getattr(error, collection._FETCH_REDIRECT_ATTR) == 1
    assert getattr(error, collection._FETCH_FAILURE_KIND_ATTR) == expected_kind
    assert _last_created(created).calls == 1


def test_fetch_feed_classifies_oversized_payload_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _install_opener(monkeypatch, response=_OversizedResponse())
    ticks = iter((30.0, 30.010))
    monkeypatch.setattr(collection.time, "monotonic", lambda: next(ticks))

    with pytest.raises(ValueError) as raised:
        collection.fetch_feed("https://example.test/feed.xml")

    assert getattr(raised.value, collection._FETCH_FAILURE_KIND_ATTR) == "payload_too_large"
    assert _last_created(created).calls == 1


def test_fetch_feed_classifies_invalid_gzip_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _Response(b"not-gzip", content_encoding="gzip")
    created = _install_opener(monkeypatch, response=response)
    ticks = iter((40.0, 40.020))
    monkeypatch.setattr(collection.time, "monotonic", lambda: next(ticks))

    with pytest.raises(ValueError) as raised:
        collection.fetch_feed("https://example.test/feed.xml")

    assert getattr(raised.value, collection._FETCH_FAILURE_KIND_ATTR) == "invalid_gzip"
    assert _last_created(created).calls == 1
