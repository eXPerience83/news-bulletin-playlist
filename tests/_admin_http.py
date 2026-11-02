"""Shared low-level HTTP and session helpers for isolated Admin handler tests."""

from __future__ import annotations

import http.client
import re
import urllib.parse
from http import HTTPStatus
from http.server import HTTPServer

_TEST_ADMIN_PASSWORD = "long-enough-admin-password"


def request(
    server: HTTPServer,
    method: str,
    path: str,
    *,
    cookie: str | None = None,
    form: dict[str, str | list[str]] | None = None,
    timeout: int = 5,
    doseq: bool = True,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
    headers = {} if cookie is None else {"Cookie": cookie}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form, doseq=doseq)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def login(server: HTTPServer, *, password: str = _TEST_ADMIN_PASSWORD, timeout: int = 5) -> str:
    status, headers, _ = request(
        server,
        "POST",
        "/admin/login",
        form={"username": "admin", "password": password},
        timeout=timeout,
    )
    assert status == HTTPStatus.SEE_OTHER
    return headers["Set-Cookie"].split(";", 1)[0]


def csrf(body: bytes) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', body)
    assert match is not None
    return match.group(1).decode()
