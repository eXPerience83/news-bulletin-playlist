from __future__ import annotations

import pytest

from news_bulletin_playlist.engine_runtime import OperationalHealthHandler
from news_bulletin_playlist.runtime import HealthHandler


class _HeaderCapture:
    def __init__(self) -> None:
        self.sent_headers: list[tuple[str, str]] = []
        self.headers_ended = False

    def send_header(self, keyword: str, value: str) -> None:
        self.sent_headers.append((keyword, value))

    def end_headers(self) -> None:
        self.headers_ended = True


class _BaseHarness(_HeaderCapture, HealthHandler):
    pass


class _OperationalHarness(_HeaderCapture, OperationalHealthHandler):
    pass


def _capture(handler: _BaseHarness | _OperationalHarness) -> list[tuple[str, str]]:
    handler._send_headers(
        content_type="text/plain; charset=utf-8",
        length=3,
        extra_headers={"X-Test-Header": "present"},
    )
    assert handler.headers_ended
    return handler.sent_headers


def test_base_handler_security_headers_remain_exact() -> None:
    assert _capture(_BaseHarness()) == [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", "3"),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        (
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        ),
        ("X-Test-Header", "present"),
    ]


def test_operational_handler_only_extends_csp_for_bundled_images() -> None:
    assert _capture(_OperationalHarness()) == [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", "3"),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        (
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        ),
        ("X-Test-Header", "present"),
    ]


@pytest.mark.parametrize("handler_type", [_BaseHarness, _OperationalHarness])
@pytest.mark.parametrize(
    ("content_type", "extra_headers"),
    [
        ("text/plain\r\nX-Invalid: yes", {}),
        ("text/plain", {"X-Invalid\r\nX-Other": "yes"}),
        ("text/plain", {"Location": "/admin/\r\nX-Invalid: yes"}),
        ("text/plain", {"X-Valid": "ok", "X-Invalid": "line\nbreak"}),
        ("text/plain", {"Bad Name": "value"}),
        ("text/plain", {"X-Invalid": "nul\x00byte"}),
        ("text/plain", {"X-Invalid": "non-latin-1 \u2603"}),
    ],
)
def test_invalid_response_headers_are_rejected_before_emission(
    handler_type, content_type, extra_headers
):
    handler = handler_type()
    with pytest.raises(ValueError, match="Invalid HTTP response header"):
        handler._send_headers(content_type=content_type, length=0, extra_headers=extra_headers)
    assert handler.sent_headers == []
    assert not handler.headers_ended


def test_valid_dynamic_headers_are_preserved() -> None:
    handler = _OperationalHarness()
    extra_headers = {
        "Location": "/admin/?notice=hello%0D%0A",
        "Set-Cookie": "session=abc; Path=/admin; HttpOnly; SameSite=Lax",
        "Content-Disposition": 'attachment; filename="configuration-v1.zip"',
    }
    handler._send_headers(content_type="application/zip", length=0, extra_headers=extra_headers)
    assert handler.sent_headers[-len(extra_headers) :] == list(extra_headers.items())
    assert handler.headers_ended
