"""Fresh v11 sources stay empty until an authenticated local JSON preview/apply."""

from __future__ import annotations

from pathlib import Path

import pytest
from _html import assert_valid_admin_html
from test_source_admin_http import _Handler, _login, _request, _server
from test_source_import_http import ROOT, _form, _post

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.effective_config import load_effective_config
from news_bulletin_playlist.portable_sources import PortableSourceError, parse_sources
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME

PRESET = "/admin/sources/preset.json?source_id=ser"


def test_empty_fresh_root_shows_downloadable_examples_without_oauth(
    tmp_path: Path,
) -> None:
    store = open_managed_state_store(tmp_path)
    assert store.catalog.sources == ()
    assert store.load_readonly().sources == ()
    with _server(tmp_path) as (server, client, _):
        _Handler.managed_admin_service = None
        _Handler.source_admin_service = None
        cookie = _login(server)
        before = (tmp_path / V11_OPERATIONAL_FILENAME).read_bytes()
        assert _request(server, "GET", PRESET)[0] != 200
        status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
        assert status == 200
        assert_valid_admin_html(page.decode())
        assert b"Available source examples" in page
        assert b"Download source preset (JSON)" in page
        assert b"Import sources" in page
        assert b"Add source</a>" not in page
        status, headers, payload = _request(server, "GET", PRESET, cookie=cookie)
        assert status == 200
        assert headers["Content-Type"] == "application/json"
        assert headers["Content-Disposition"] == 'attachment; filename="preset-ser.json"'
        (record,) = parse_sources(payload)
        assert record.source == BUILTIN_CATALOG.sources[0]
        assert record.provenance == "built_in"
        assert not client.calls
        assert (tmp_path / V11_OPERATIONAL_FILENAME).read_bytes() == before
        assert store.load_readonly().sources == ()


def test_explicit_preset_import_without_oauth_is_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    open_managed_state_store(tmp_path)
    with _server(tmp_path) as (server, client, _):
        _Handler.managed_admin_service = None
        _Handler.source_admin_service = None
        cookie = _login(server)
        source_json = _request(server, "GET", PRESET, cookie=cookie)[2]
        page = _request(server, "GET", ROOT, cookie=cookie)[2]
        from test_source_admin_http import _csrf

        status, _, preview = _post(server, cookie, source_json, _csrf(page))
        assert status == 200
        assert b"create" in preview
        assert open_managed_state_store(tmp_path).load_readonly().sources == ()
        status, _, result = _request(
            server, "POST", ROOT + "/apply", cookie=cookie, form=_form(preview)
        )
        assert status == 200, result
        assert b"1 source(s) installed" in result
        installed = open_managed_state_store(tmp_path).load_readonly()
        assert len(installed.sources) == 1
        assert installed.sources[0].spotify_show_id == (
            BUILTIN_CATALOG.sources[0].external_references[0].external_id
        )
        assert load_effective_config(tmp_path, {}) is None  # No destination created.
        assert open_managed_state_store(tmp_path).load_readonly() == installed
        assert client.calls == []
        page = _request(server, "GET", ROOT, cookie=cookie)[2]
        status, _, preview = _post(server, cookie, source_json, _csrf(page))
        assert status == 200
        assert b"reuse" in preview
        status, _, result = _request(
            server, "POST", ROOT + "/apply", cookie=cookie, form=_form(preview)
        )
        assert status == 200
        assert open_managed_state_store(tmp_path).load_readonly() == installed


@pytest.mark.parametrize(
    "query",
    [
        "",
        "source_id=",
        "source_id=unknown",
        "source_id=ser&source_id=rne",
        "source_id=../ser",
        "source_id=ser&other=1",
    ],
)
def test_invalid_preset_selection_cannot_mutate_state(tmp_path: Path, query: str) -> None:
    open_managed_state_store(tmp_path)
    with _server(tmp_path) as (server, _, _):
        _Handler.managed_admin_service = None
        _Handler.source_admin_service = None
        cookie = _login(server)
        before = (tmp_path / V11_OPERATIONAL_FILENAME).read_bytes()
        response = _request(server, "GET", "/admin/sources/preset.json?" + query, cookie=cookie)
        assert response[0] == 400
        assert (tmp_path / V11_OPERATIONAL_FILENAME).read_bytes() == before


def test_regular_portable_import_does_not_allow_zero_source_document() -> None:
    import json

    empty_document = json.dumps(
        {"format": "news-bulletin-playlist.sources", "version": 1, "sources": []}
    ).encode()
    with pytest.raises(PortableSourceError):
        parse_sources(empty_document)
