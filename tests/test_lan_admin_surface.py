from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import HTTPServer
from pathlib import Path

import pytest

from news_bulletin_playlist.lan_admin import LanAdminHandler, LanAdminSecurity


class _Handler(LanAdminHandler):
    pass


def test_lan_mode_closes_direct_get_callback_route(tmp_path: Path) -> None:
    _Handler.data_dir = tmp_path
    _Handler.admin_security = LanAdminSecurity("long-enough-admin-password")
    _Handler.spotify_auth = None
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/admin/spotify/callback?state=s&code=c"
    )
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        assert raised.value.code == HTTPStatus.NOT_FOUND
        assert raised.value.headers.get("WWW-Authenticate") is None
        assert raised.value.read() == b"Not found"
    finally:
        thread.join(timeout=2)
        server.server_close()
        _Handler.admin_security = None
        _Handler.spotify_auth = None
