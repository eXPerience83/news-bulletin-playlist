from __future__ import annotations

import base64
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPMessage
from typing import Any

from news_bulletin_playlist.admin_session import (
    ADMIN_SESSION_COOKIE,
    admin_session_manager_for,
)


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
    handler._form = {} if form is None else {key: list(values) for key, values in form.items()}
    if password is not None:
        security = handler.admin_security
        assert security is not None
        assert security.is_authorized(basic_header(password))
        manager = admin_session_manager_for(security)
        session_id = getattr(security, "_test_admin_session_id", None)
        cookie_header = None if session_id is None else f"{ADMIN_SESSION_COOKIE}={session_id}"
        if manager.resolve_session(cookie_header) is None:
            session_id = manager.create_session()
            security._test_admin_session_id = session_id
        handler.headers["Cookie"] = f"{ADMIN_SESSION_COOKIE}={session_id}"

        # Older unit fixtures create a CSRF token directly on AdminSecurity before constructing
        # the handler. Translate only a still-valid legacy fixture token into the equivalent
        # session-scoped token so production code has one CSRF acceptance path.
        csrf_values = handler._form.get("csrf_token", [])
        if len(csrf_values) == 1 and security.consume_csrf_token(csrf_values[0]):
            handler._form["csrf_token"] = [manager.issue_csrf_token(session_id)]
    handler.response = None


def captured_response(handler: HandlerCaptureMixin) -> CapturedResponse:
    assert handler.response is not None
    return handler.response
