from __future__ import annotations

import base64
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPMessage
from typing import Any


@dataclass(frozen=True, slots=True)
class CapturedResponse:
    status: HTTPStatus
    payload: bytes
    content_type: str
    extra_headers: dict[str, str]


class HandlerCaptureMixin:
    """Test-only in-memory form/response capture for HTTP handler subclasses."""

    _form: dict[str, list[str]]
    response: CapturedResponse | None

    def _read_form(self) -> dict[str, list[str]]:
        return self._form

    def _reply(
        self,
        status: HTTPStatus,
        payload: bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.response = CapturedResponse(
            status=status,
            payload=payload,
            content_type=content_type,
            extra_headers={} if extra_headers is None else dict(extra_headers),
        )


def basic_header(password: str) -> str:
    encoded = base64.b64encode(f"admin:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


def initialize_handler_capture(
    handler: Any,
    *,
    path: str,
    form: dict[str, list[str]] | None = None,
    password: str | None = None,
    client_ip: str = "127.0.0.1",
) -> None:
    handler.path = path
    handler.client_address = (client_ip, 12345)
    handler.headers = HTTPMessage()
    if password is not None:
        handler.headers["Authorization"] = basic_header(password)
    handler._form = {} if form is None else form
    handler.response = None


def captured_response(handler: HandlerCaptureMixin) -> CapturedResponse:
    assert handler.response is not None
    return handler.response
