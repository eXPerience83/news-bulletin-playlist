import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from news_bulletin_playlist import collection


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/start":
            self.send_response(301)
            self.send_header("Location", "/feed")
            self.end_headers()
            return
        if self.path == "/loop":
            self.send_response(301)
            self.send_header("Location", "/loop")
            self.end_headers()
            return
        if self.path == "/feed":
            payload = b"<rss />"
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _local_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_fetch_feed_counts_followed_redirect_with_real_http() -> None:
    with _local_server() as base_url:
        result = collection.fetch_feed(f"{base_url}/start")

    assert result == b"<rss />"
    assert isinstance(result, collection._ObservedFeedPayload)
    assert result.rss_redirect_count == 1


def test_fetch_feed_does_not_count_rejected_loop_redirect() -> None:
    with _local_server() as base_url, pytest.raises(urllib.error.HTTPError) as raised:
        collection.fetch_feed(f"{base_url}/loop")

    assert raised.value.code == 301
    assert getattr(raised.value, collection._FETCH_REDIRECT_ATTR) == 4
    assert getattr(raised.value, collection._FETCH_FAILURE_KIND_ATTR) == "redirect_error"
