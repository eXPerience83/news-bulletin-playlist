"""Authenticated source upload, immutable preview and session-bound explicit apply."""

from __future__ import annotations

import http.client
import json
import re
from dataclasses import replace

import pytest
from _html import assert_valid_admin_html
from test_installed_sources import _rich_state
from test_source_admin_http import _csrf, _Handler, _login, _request, _server
from test_source_configuration_export import _files, _forbid_effects

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_provisioning import ProvisioningIntent, ProvisioningState
from news_bulletin_playlist.managed_state import ManagedState, ManagedStateStore
from news_bulletin_playlist.models import ExternalReference, SourceId
from news_bulletin_playlist.portable_sources import export_sources

ROOT = "/admin/sources/import"


def _document(**changes):
    source = replace(
        BUILTIN_CATALOG.sources[0],
        id=SourceId("foreign_source"),
        external_references=(ExternalReference("spotify", "show", "B" * 22),),
    )
    return export_sources((replace(source, **changes),))


def _multipart(token, document):
    boundary = "source-import-test-boundary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n{token}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
            'filename="../../managed-state.json"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode()
        + document
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return "multipart/form-data; boundary=" + boundary, body


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


@pytest.fixture
def installation(tmp_path):
    with _server(tmp_path) as (server, client, store):
        state = _rich_state()
        store.save(state)
        service = _Handler.source_admin_service
        service.provisioning_journal.save(
            ProvisioningIntent(
                ProvisioningState.REQUEST_STARTED,
                BUILTIN_CATALOG.playlists[0].id,
                "Pending",
                "Keep description",
                "spain_spanish_news",
                (SourceId("ser"),),
                retention_hours=48,
                max_episodes=50,
                max_duration_seconds=900,
            )
        )
        (tmp_path / "oauth-sentinel.json").write_text('{"token":"private-token-sentinel"}')
        cookie = _login(server)
        status, _, page = _request(server, "GET", ROOT, cookie=cookie)
        assert status == 200
        yield server, cookie, store, service, client, state, page


def _guard(monkeypatch, service):
    save = ManagedStateStore.save
    _forbid_effects(monkeypatch, service)
    flags = {"allow_save": False, "saves": 0}

    def guarded(store, state):
        if not flags["allow_save"]:
            pytest.fail("Preview/no-op must not save")
        flags["saves"] += 1
        return save(store, state)

    monkeypatch.setattr(ManagedStateStore, "save", guarded)
    return flags


def test_sources_page_links_to_import_and_upload_form_is_private(installation):
    server, cookie, store, service, client, state, page = installation
    assert b'type="file"' in page and b'enctype="multipart/form-data"' in page
    assert_valid_admin_html(page.decode())
    status, headers, page = _request(server, "GET", ROOT, cookie=cookie)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    _, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
    assert b'href="/admin/sources/import">Import sources</a>' in page


def test_preview_is_readonly_and_explicit_apply_preserves_all_other_files(
    installation, monkeypatch
):
    server, cookie, store, service, client, state, page = installation
    before = _files(store.path.parent)
    flags = _guard(monkeypatch, service)
    status, headers, body = _post(server, cookie, _document(), _csrf(page))
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert b"create" in body and b"Nothing has been installed" in body
    assert_valid_admin_html(body.decode())
    assert _files(store.path.parent) == before
    flags["allow_save"] = True
    status, _, result = _request(server, "POST", ROOT + "/apply", cookie=cookie, form=_form(body))
    assert status == 200 and b"1 source(s) installed" in result
    assert_valid_admin_html(result.decode())
    assert flags["saves"] == 1
    loaded = store.load_readonly()
    assert replace(loaded, sources=state.sources) == state
    after = _files(store.path.parent)
    assert {name: data for name, data in after.items() if name != store.path.name} == {
        name: data for name, data in before.items() if name != store.path.name
    }
    assert client.calls == []
    # Replay is CSRF-rejected and cannot save a second time.
    assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=_form(body))[0] == 403
    assert flags["saves"] == 1
    # A fresh preview of the same document is a true no-op.
    _, _, page = _request(server, "GET", ROOT, cookie=cookie)
    status, _, body = _post(server, cookie, _document(), _csrf(page))
    assert status == 200 and b"reuse" in body
    flags["allow_save"] = False
    assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=_form(body))[0] == 200
    assert _files(store.path.parent) == after


@pytest.mark.parametrize(
    "method,path", [("GET", ROOT), ("POST", ROOT + "/preview"), ("POST", ROOT + "/apply")]
)
def test_unauthenticated_import_never_reads_state(installation, monkeypatch, method, path):
    server, cookie, store, service, client, state, page = installation

    def forbidden(*args, **kwargs):
        pytest.fail("Unauthenticated import read state")

    monkeypatch.setattr(ManagedStateStore, "load_readonly", forbidden)
    if path.endswith("/preview"):
        status = _post(server, None, _document(), _csrf(page))[0]
    else:
        status = _request(
            server, method, path, form={"csrf_token": "bad"} if method == "POST" else None
        )[0]
    assert status == 303


@pytest.mark.parametrize("stage", ["preview", "apply"])
def test_csrf_is_required_before_import_state_access(installation, monkeypatch, stage):
    server, cookie, store, service, client, state, page = installation
    body = page
    if stage == "apply":
        status, _, body = _post(server, cookie, _document(), _csrf(page))
        assert status == 200
    before = _files(store.path.parent)
    _guard(monkeypatch, service)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid CSRF must not read configuration")

    monkeypatch.setattr(store, "load_readonly", forbidden)
    if stage == "preview":
        status = _post(server, cookie, _document(), "x" * 32)[0]
    else:
        form = _form(body)
        form["csrf_token"] = "x" * 32
        status = _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form)[0]
    assert status == 403
    assert _files(store.path.parent) == before


def test_two_sessions_cannot_confirm_each_others_preview(installation, monkeypatch):
    server, cookie, store, service, client, state, page = installation
    second_cookie = _login(server)
    _, _, second_page = _request(server, "GET", ROOT, cookie=second_cookie)
    _guard(monkeypatch, service)
    status, _, body = _post(server, cookie, _document(), _csrf(page))
    assert status == 200
    form = _form(body)
    form["csrf_token"] = _csrf(second_page)
    assert _request(server, "POST", ROOT + "/apply", cookie=second_cookie, form=form)[0] == 409
    # The failed other-session request did not consume the rightful owner's context.
    form = _form(body)
    form["action"] = "cancel"
    assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form)[0] == 200
    assert client.calls == []


def test_superseded_preview_cannot_confirm_a_new_document(installation, monkeypatch):
    server, cookie, store, service, client, state, page = installation
    _guard(monkeypatch, service)
    _, _, first = _post(server, cookie, _document(display_name="First"), _csrf(page))
    _, _, second = _post(server, cookie, _document(display_name="Second"), _csrf(first))
    form = _form(first)
    form["csrf_token"] = _csrf(second)
    assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form)[0] == 409


@pytest.mark.parametrize("change", ["state", "pending"])
def test_stale_http_confirmation_requires_new_preview(installation, monkeypatch, change):
    server, cookie, store, service, client, state, page = installation
    status, _, body = _post(server, cookie, _document(), _csrf(page))
    assert status == 200
    if change == "state":
        store.save(replace(state, quarantined_templates=()))
    else:
        intent = service.provisioning_journal.load()
        service.provisioning_journal.save(replace(intent, display_name="Changed pending"))
    before = _files(store.path.parent)
    _guard(monkeypatch, service)
    assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=_form(body))[0] == 409
    assert _files(store.path.parent) == before


@pytest.mark.parametrize(
    "change", ["document", "unknown", "missing_confirm", "bad_decision", "duplicate_field"]
)
def test_tampered_confirmation_does_not_install(installation, monkeypatch, change):
    server, cookie, store, service, client, state, page = installation
    _guard(monkeypatch, service)
    status, _, body = _post(server, cookie, _document(), _csrf(page))
    assert status == 200
    before = _files(store.path.parent)
    form = _form(body)
    if change == "document":
        form["document"] = _document().decode()
    elif change == "unknown":
        form["replace_local"] = "1"
    elif change == "missing_confirm":
        form.pop("confirm")
    elif change == "bad_decision":
        form["decision_0"] = "keep_local"
    else:
        form["confirm"] = ["1", "1"]
    status = _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form)[0]
    assert status in {400, 409}
    assert _files(store.path.parent) == before


def test_keep_local_conflict_and_cancel_are_explicit_no_ops(installation, monkeypatch):
    server, cookie, store, service, client, state, page = installation
    document = export_sources((replace(BUILTIN_CATALOG.sources[0], display_name="Foreign name"),))
    before = _files(store.path.parent)
    _guard(monkeypatch, service)
    status, _, body = _post(server, cookie, document, _csrf(page))
    assert status == 200
    assert b"conflict" in body and b"Foreign name" in body and b"Cadena SER" in body
    form = _form(body)
    form["decision_0"] = "keep_local"
    form.pop("ack_v8")
    assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form)[0] == 200
    assert _files(store.path.parent) == before
    _, _, page = _request(server, "GET", ROOT, cookie=cookie)
    _, _, body = _post(server, cookie, _document(), _csrf(page))
    form = _form(body)
    form["action"] = "cancel"
    form.pop("confirm")
    assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form)[0] == 200
    assert _files(store.path.parent) == before


@pytest.mark.parametrize("version", [6, 7])
def test_http_schema_upgrade_warning_and_acknowledgement(installation, monkeypatch, version):
    server, cookie, store, service, client, state, page = installation
    state = ManagedState(schema_version=version)
    store.save(state)
    before = _files(store.path.parent)
    flags = _guard(monkeypatch, service)
    _, _, body = _post(server, cookie, _document(), _csrf(page))
    assert b"Older images cannot" in body and b"complete production backup" in body
    form = _form(body)
    form.pop("ack_v8")
    assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form)[0] == 409
    assert _files(store.path.parent) == before
    _, _, page = _request(server, "GET", ROOT, cookie=cookie)
    _, _, body = _post(server, cookie, _document(), _csrf(page))
    flags["allow_save"] = True
    assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=_form(body))[0] == 200
    assert store.load_readonly().schema_version == 8 and flags["saves"] == 1


@pytest.mark.parametrize(
    "document,expected",
    [(b"{", 400), (b"x" * (256 * 1024 + 1), 400), (b"x" * (256 * 1024 + 8193), 413)],
)
def test_bad_or_oversized_upload_has_no_side_effects(installation, monkeypatch, document, expected):
    server, cookie, store, service, client, state, page = installation
    before = _files(store.path.parent)
    _guard(monkeypatch, service)
    assert _post(server, cookie, document, _csrf(page))[0] == expected
    assert _files(store.path.parent) == before


def test_unsupported_capability_returns_sanitized_error(installation, monkeypatch):
    server, cookie, store, service, client, state, page = installation
    _guard(monkeypatch, service)
    payload = json.loads(_document())
    payload["sources"][0]["processing"]["parser"]["id"] = "private-error-sentinel"
    status, _, body = _post(server, cookie, json.dumps(payload).encode(), _csrf(page))
    assert status == 422 and b"private-error-sentinel" not in body


def test_import_escapes_untrusted_names_and_is_available_without_oauth_service(
    installation, monkeypatch
):
    server, cookie, store, service, client, state, page = installation
    flags = _guard(monkeypatch, service)
    monkeypatch.setattr(_Handler, "source_admin_service", None)
    monkeypatch.setattr(_Handler, "managed_admin_service", None)
    name = '<script>alert("x")</script>'
    status, _, body = _post(server, cookie, _document(display_name=name), _csrf(page))
    assert status == 200
    assert name.encode() not in body and b"&lt;script&gt;" in body
    assert_valid_admin_html(body.decode())
    flags["allow_save"] = True
    status, _, body = _request(server, "POST", ROOT + "/apply", cookie=cookie, form=_form(body))
    assert status == 200 and client.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Transfer-Encoding": "chunked"},
        {"Content-Length": "-1"},
        {"Content-Length": "abc"},
        {"Content-Length": "0"},
        {"Content-Type": "application/json"},
    ],
)
def test_strict_upload_headers_are_rejected_before_configuration(
    installation, monkeypatch, headers
):
    server, cookie, store, service, client, state, page = installation
    _guard(monkeypatch, service)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid upload must not read state")

    monkeypatch.setattr(store, "load_readonly", forbidden)
    assert _post(server, cookie, _document(), _csrf(page), modify=headers)[0] in {400, 413}


def test_post_replace_error_is_reported_with_readback_and_without_second_save(
    installation, monkeypatch
):
    from news_bulletin_playlist import managed_state

    server, cookie, store, service, client, state, page = installation
    flags = _guard(monkeypatch, service)
    _, _, body = _post(server, cookie, _document(), _csrf(page))

    def interrupted(*args, **kwargs):
        raise OSError("private-storage-sentinel")

    monkeypatch.setattr(managed_state, "fsync_directory", interrupted)
    flags["allow_save"] = True
    status, _, result = _request(server, "POST", ROOT + "/apply", cookie=cookie, form=_form(body))
    assert status == 200 and b"Readback confirms" in result
    assert b"private-storage-sentinel" not in result and flags["saves"] == 1
    assert len(store.load_readonly().sources) == len(state.sources) + 1


def test_full_hundred_conflict_decisions_fit_existing_confirmation_form_limit(
    installation, monkeypatch
):
    from test_managed_sources import _source

    from news_bulletin_playlist.managed_state import InstalledSource, source_definition

    server, cookie, store, service, client, state, page = installation
    definitions = tuple(source_definition(_source("I" + str(i).zfill(21))) for i in range(100))
    store.save(
        ManagedState(
            schema_version=8, sources=tuple(InstalledSource.from_definition(s) for s in definitions)
        )
    )
    document = export_sources(
        tuple(replace(s, display_name="Foreign " + str(i)) for i, s in enumerate(definitions))
    )
    _guard(monkeypatch, service)
    status, _, body = _post(server, cookie, document, _csrf(page))
    assert status == 200
    form = _form(body)
    form.update({f"decision_{i}": "keep_local" for i in range(100)})
    status, _, result = _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form)
    assert status == 200 and b"No configuration was written" in result


def test_preview_capacity_returns_retry_guidance_without_writes(installation, monkeypatch):
    from news_bulletin_playlist.admin_session import (
        MAX_SOURCE_IMPORT_PREVIEWS,
        admin_session_manager_for,
    )

    server, cookie, store, service, client, state, page = installation
    manager = admin_session_manager_for(_Handler.admin_security)
    occupied = []
    for _ in range(MAX_SOURCE_IMPORT_PREVIEWS):
        session = manager.create_session()
        occupied.append(session)
        manager.stage_source_import(session, _document(), "configuration")
    before = _files(store.path.parent)
    _guard(monkeypatch, service)
    status, _, body = _post(server, cookie, _document(), _csrf(page))
    assert status == 503
    assert b"Too many source previews; cancel an existing preview or try later" in body
    assert _files(store.path.parent) == before
    # Once a slot is freed, the operator can submit a new preview normally.
    manager.invalidate_session(occupied[0])
    _, _, page = _request(server, "GET", ROOT, cookie=cookie)
    assert _post(server, cookie, _document(), _csrf(page))[0] == 200
    assert _files(store.path.parent) == before
