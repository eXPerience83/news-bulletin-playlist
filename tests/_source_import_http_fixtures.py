"""HTTP upload and confirmation helpers for offline Source Import tests."""

from __future__ import annotations

import http.client
import re

from _admin_http import csrf as _csrf
from _multipart import import_upload as _multipart

ROOT = "/admin/sources/import"


def _post(server, cookie, document, token, *, path=ROOT + "/preview", modify=None):
    content_type, body = _multipart(token, document)
    headers = {"Content-Type": content_type}
    if cookie:
        headers["Cookie"] = cookie
    if modify:
        headers.update(modify)
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    client.request("POST", path, body=body, headers=headers)
    response = client.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    client.close()
    return result


def _form(body, **changes):
    match = re.search(rb'name="preview_id" value="([A-Za-z0-9_-]+)"', body)
    assert match
    return dict(
        csrf_token=_csrf(body),
        preview_id=match[1].decode(),
        action="apply",
        confirm="1",
        ack_v8="1",
        **changes,
    )
