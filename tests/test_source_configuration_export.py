from __future__ import annotations

import json
import socket
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from http import HTTPStatus

import pytest
from _html import assert_valid_admin_html
from test_managed_sources import _source
from test_source_admin_http import _Handler, _login, _request, _server

from news_bulletin_playlist import diagnostics_runtime
from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_provisioning import ProvisioningIntent, ProvisioningState
from news_bulletin_playlist.managed_state import (
    ManagedSourceOverride,
    ManagedState,
    ManagedStateStore,
    effective_sources,
    serialize_managed_state,
)
from news_bulletin_playlist.models import CountryCode, LanguageTag, PublicReleaseMode, SourceId
from news_bulletin_playlist.portable_sources import parse_sources
from news_bulletin_playlist.registry import TITLE_PARSERS
from news_bulletin_playlist.source_admin import SourceAdminService

EXPORT = "/admin/sources/export.json"


@pytest.fixture
def installation(tmp_path):
    with _server(tmp_path) as (server, client, store):
        state = ManagedState(
            sources=(_source(enabled=False),),
            source_overrides=(ManagedSourceOverride(SourceId("rne"), PublicReleaseMode.DISABLED),),
        )
        store.save(state)
        service = _Handler.source_admin_service
        template = BUILTIN_CATALOG.playlists[0]
        service.provisioning_journal.save(
            ProvisioningIntent(
                state=ProvisioningState.DESTINATION_KNOWN,
                template_id=template.id,
                display_name="Pending destination",
                description="private-description-sentinel",
                cover_id=template.cover_id,
                source_ids=template.default_source_ids,
                retention_hours=48,
                max_episodes=100,
                max_duration_seconds=1800,
                destination_id="private-destination-sentinel",
            )
        )
        (tmp_path / "oauth-sentinel.json").write_text('{"token":"private-token-sentinel"}')
        cookie = _login(server)
        # The login response precedes its handler's auth-state epilogue. A static
        # GET on the same single-thread server lets that epilogue finish before
        # tests install guards against auth/operational activity during downloads.
        assert _request(server, "GET", "/admin/sources/new", cookie=cookie)[0] == HTTPStatus.OK
        yield server, cookie, store, service, client, state


def _files(root):
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def _forbid_effects(monkeypatch, service):
    def forbidden(*args, **kwargs):
        pytest.fail("Portable download attempted an operational side effect")

    for cls, names in (
        (ManagedStateStore, ("load", "save")),
        (ManagedAdminService, ("snapshot", "finalize_known_provisioning")),
        (SourceAdminService, ("snapshot", "inspect", "audit", "audit_all")),
    ):
        for name in names:
            monkeypatch.setattr(cls, name, forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(service.auth, "get_access_token", forbidden)
    monkeypatch.setattr(service, "client_factory", forbidden)
    monkeypatch.setattr(service.public_client, "observe_episode", forbidden)
    monkeypatch.setattr(_Handler, "_spotify_state", forbidden)
    monkeypatch.setattr(_Handler, "_spotify_metrics", forbidden)
    monkeypatch.setattr(_Handler, "_admin_diagnostics", forbidden)
    monkeypatch.setattr(_Handler.engine_lifecycle, "reconcile", forbidden)
    monkeypatch.setattr(_Handler.engine_lifecycle, "wake", forbidden, raising=False)
    connect = socket.socket.connect

    def local_only(sock, address):
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            forbidden()
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)


def test_all_sources_export_is_lossless_deterministic_and_readonly(installation, monkeypatch):
    server, cookie, store, service, client, state = installation
    before = _files(store.path.parent)
    _forbid_effects(monkeypatch, service)
    status, headers, body = _request(server, "GET", EXPORT, cookie=cookie)
    assert status == HTTPStatus.OK
    assert headers["Content-Type"] == "application/json"
    assert headers["Content-Disposition"] == 'attachment; filename="sources-v1.json"'
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert int(headers["Content-Length"]) == len(body)
    parsed = parse_sources(body)
    assert len(parsed) == 14
    actual = {item.source.id: item.source for item in parsed}
    assert actual == {s.id: s for s in effective_sources(BUILTIN_CATALOG, state)}
    assert {item.source.id for item in parsed if item.provenance == "built_in"} == {
        s.id for s in BUILTIN_CATALOG.sources
    }
    assert not actual[state.sources[0].id].enabled
    assert actual[SourceId("rne")].public_release_mode is PublicReleaseMode.DISABLED
    assert actual[SourceId("un_news_es")].collection_filter_id == "un_news_es_minutes"
    assert len({s.external_references[0].external_id for s in actual.values()}) == 14
    assert b"private-" not in body
    assert _request(server, "GET", EXPORT, cookie=cookie)[2] == body
    assert _files(store.path.parent) == before
    assert client.calls == []


@pytest.mark.parametrize(
    "source_id",
    [str(s.id) for s in BUILTIN_CATALOG.sources]
    + [
        "custom_" + "A" * 22,
    ],
)
def test_individual_source_download_preserves_every_capability(
    installation, monkeypatch, source_id
):
    server, cookie, store, service, _, state = installation
    before = _files(store.path.parent)
    _forbid_effects(monkeypatch, service)
    status, headers, body = _request(
        server, "GET", f"{EXPORT}?source_id={source_id}", cookie=cookie
    )
    assert status == HTTPStatus.OK
    assert headers["Content-Disposition"] == f'attachment; filename="source-{source_id}-v1.json"'
    (decoded,) = parse_sources(body)
    assert decoded.source == next(
        s for s in effective_sources(BUILTIN_CATALOG, state) if s.id == source_id
    )
    assert _files(store.path.parent) == before


def test_multiple_languages_and_countries_survive_http(installation):
    server, cookie, _, service, _, _ = installation
    source = replace(
        BUILTIN_CATALOG.sources[0],
        countries=(CountryCode("ES"), CountryCode("GB")),
        languages=(LanguageTag("es-ES"), LanguageTag("en")),
    )
    _Handler.managed_admin_service.catalog = replace(
        BUILTIN_CATALOG, sources=(source, *BUILTIN_CATALOG.sources[1:])
    )
    status, _, body = _request(server, "GET", f"{EXPORT}?source_id=ser", cookie=cookie)
    assert status == HTTPStatus.OK
    assert parse_sources(body)[0].source == source


@pytest.mark.parametrize(
    "query",
    [
        "source_id=",
        "source_id=ser&source_id=ser",
        "source_id=ser&source_id=rne",
        "source_id=ser&%73ource_id=ser",
        "other=ser",
        "source_id=ser&other=value",
        "source_id",
        "source_id=../escape",
        "source_id=%0d%0aInjected",
        "source_id=%FF",
        "source_id=%",
        "source_id=ser%2Fpath",
        "source_id=" + "a" * 129,
        "source_id=" + "a" * 513,
    ],
)
def test_invalid_or_ambiguous_selection_is_rejected_without_reading(
    installation, monkeypatch, query
):
    server, cookie, _, service, _, _ = installation

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid selection must not read configuration")

    monkeypatch.setattr(diagnostics_runtime, "export_source_configuration", forbidden)
    status, headers, body = _request(server, "GET", f"{EXPORT}?{query}", cookie=cookie)
    assert status == HTTPStatus.BAD_REQUEST
    assert "Content-Disposition" not in headers
    assert body == b"Invalid source export selection"


def test_unknown_source_is_404_without_changes(installation, monkeypatch):
    server, cookie, store, service, _, _ = installation
    before = _files(store.path.parent)
    _forbid_effects(monkeypatch, service)
    status, _, body = _request(server, "GET", f"{EXPORT}?source_id=missing", cookie=cookie)
    assert status == HTTPStatus.NOT_FOUND
    assert body == b"Configured source not found"
    assert _files(store.path.parent) == before


@pytest.mark.parametrize("query", ["", "?source_id=ser", "?source_id=ser&source_id=ser"])
def test_unauthenticated_download_never_reads_configuration(installation, monkeypatch, query):
    server, _, _, service, _, _ = installation

    def forbidden(*args, **kwargs):
        pytest.fail("Unauthenticated request must not read configuration")

    monkeypatch.setattr(diagnostics_runtime, "export_source_configuration", forbidden)
    status, headers, body = _request(server, "GET", EXPORT + query)
    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/admin/login"
    assert "Content-Disposition" not in headers
    assert not body


@pytest.mark.parametrize("version", range(1, 8))
def test_legacy_download_upgrades_only_in_memory(installation, monkeypatch, version):
    server, cookie, store, service, _, state = installation
    payload = serialize_managed_state(state)
    payload["schema_version"] = version
    if version < 3:
        payload.pop("sources")
    if version < 5:
        payload.pop("source_overrides")
        for source in payload.get("sources", []):
            source.pop("public_release_mode")
    if version == 7:
        payload["templates"] = []
    store.path.write_text(json.dumps(payload, indent=4) + "\n\n")
    before = _files(store.path.parent)
    _forbid_effects(monkeypatch, service)
    status, _, body = _request(server, "GET", EXPORT, cookie=cookie)
    assert status == HTTPStatus.OK
    assert len(parse_sources(body)) == (13 if version < 3 else 14)
    assert _files(store.path.parent) == before


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"\xff",
        b'{"schema_version":6,"schema_version":6,"playlists":[]}',
        b'{"schema_version":999,"secret":"private-token-sentinel"}',
        b'{"schema_version":' + b"9" * 5000 + b"}",
    ],
)
def test_corrupt_state_returns_sanitized_error_without_changes(installation, monkeypatch, raw):
    server, cookie, store, service, _, _ = installation
    store.path.write_bytes(raw)
    before = _files(store.path.parent)
    _forbid_effects(monkeypatch, service)
    status, headers, body = _request(server, "GET", EXPORT, cookie=cookie)
    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert body == b"Source export is unavailable"
    assert "Content-Disposition" not in headers
    assert _files(store.path.parent) == before


@pytest.mark.parametrize("kind", ["symlink", "broken_symlink", "directory"])
def test_unsafe_state_path_is_rejected(installation, monkeypatch, kind):
    server, cookie, store, service, _, _ = installation
    original = store.path.read_bytes()
    store.path.unlink()
    target = store.path.with_name("other-installation.json")
    if kind == "directory":
        store.path.mkdir()
    else:
        if kind == "symlink":
            target.write_bytes(original)
        store.path.symlink_to(target)
    _forbid_effects(monkeypatch, service)
    status, _, body = _request(server, "GET", EXPORT, cookie=cookie)
    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert body == b"Source export is unavailable"
    if kind == "symlink":
        assert target.read_bytes() == original
    elif kind == "broken_symlink":
        assert store.path.is_symlink() and not target.exists()
    else:
        assert list(store.path.iterdir()) == []


def test_missing_state_is_not_created(installation, monkeypatch):
    server, cookie, store, service, _, _ = installation
    store.path.unlink()
    before = _files(store.path.parent)
    _forbid_effects(monkeypatch, service)
    status, _, body = _request(server, "GET", EXPORT, cookie=cookie)
    assert status == HTTPStatus.OK
    assert len(parse_sources(body)) == 13
    assert not store.path.exists()
    assert _files(store.path.parent) == before


def test_snapshot_is_read_once_under_existing_configuration_lock(installation, monkeypatch):
    server, cookie, store, service, _, _ = installation
    hold = service.synchronization.hold
    read = store.load_readonly
    held = False
    reads = 0

    @contextmanager
    def tracked_hold():
        nonlocal held
        with hold():
            held = True
            try:
                yield
            finally:
                held = False

    def tracked_read():
        nonlocal reads
        assert held
        reads += 1
        return read()

    monkeypatch.setattr(service.synchronization, "hold", tracked_hold)
    monkeypatch.setattr(store, "load_readonly", tracked_read)
    _forbid_effects(monkeypatch, service)
    assert _request(server, "GET", EXPORT, cookie=cookie)[0] == HTTPStatus.OK
    assert reads == 1


def test_sources_page_has_distinct_portable_download_links(installation):
    server, cookie, _, _, _, state = installation
    # A normal page currently uses the existing provisioning-aware snapshot.
    _Handler.managed_admin_service.provisioning_journal.clear()
    status, _, body = _request(server, "GET", "/admin/sources", cookie=cookie)
    assert status == HTTPStatus.OK
    assert_valid_admin_html(body.decode())
    assert b"Export all sources</a>" in body
    assert b"Export all audits</button>" in body
    assert body.count(b"Export source</a>") == 14
    for source in effective_sources(BUILTIN_CATALOG, state):
        assert f'href="{EXPORT}?source_id={source.id}"'.encode() in body


def test_empty_installation_explains_unavailable_export(installation):
    server, cookie, store, service, _, _ = installation
    store.save(ManagedState())
    service.provisioning_journal.clear()
    service.catalog = BuiltInCatalog((), ())
    _Handler.managed_admin_service.catalog = service.catalog
    status, _, page = _request(server, "GET", "/admin/sources", cookie=cookie)
    assert status == HTTPStatus.OK
    assert b"No sources are available to export. Add a source first." in page
    assert b"Export all sources</a>" not in page
    status, headers, body = _request(server, "GET", EXPORT, cookie=cookie)
    assert status == HTTPStatus.CONFLICT
    assert body == b"No sources are available to export"
    assert "Content-Disposition" not in headers


def test_readonly_load_reuses_legacy_parser_but_normal_load_still_persists(tmp_path):
    path = tmp_path / "managed-state.json"
    raw = b'{"schema_version":1,"playlists":[]}\n'
    path.write_bytes(raw)
    store = ManagedStateStore(path)
    assert store.load_readonly().schema_version == 6
    assert path.read_bytes() == raw
    assert store.load().schema_version == 6
    assert json.loads(path.read_bytes())["schema_version"] == 6


def test_failed_readonly_io_is_sanitized(installation, monkeypatch):
    server, cookie, _, service, _, _ = installation

    def failed_read():
        raise OSError("private-token-sentinel")

    monkeypatch.setattr(service.store, "load_readonly", failed_read)
    status, _, body = _request(server, "GET", EXPORT, cookie=cookie)
    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert body == b"Source export is unavailable"


def test_duplicate_show_state_does_not_export_partial_data(installation, monkeypatch):
    server, cookie, store, service, _, state = installation
    payload = serialize_managed_state(state)
    payload["sources"].append(payload["sources"][0])
    store.path.write_text(json.dumps(payload))
    before = _files(store.path.parent)
    _forbid_effects(monkeypatch, service)
    assert _request(server, "GET", EXPORT, cookie=cookie)[0] == HTTPStatus.INTERNAL_SERVER_ERROR
    assert _files(store.path.parent) == before


def test_codec_incompatibility_returns_no_partial_download(installation, monkeypatch):
    server, cookie, store, service, _, _ = installation
    before = _files(store.path.parent)
    _forbid_effects(monkeypatch, service)
    monkeypatch.delitem(TITLE_PARSERS, "ser")
    status, headers, body = _request(server, "GET", EXPORT, cookie=cookie)
    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert body == b"Source export is unavailable"
    assert "Content-Disposition" not in headers
    assert _files(store.path.parent) == before


def test_missing_export_synchronization_is_unavailable(installation, monkeypatch):
    server, cookie, _, _, _, _ = installation
    monkeypatch.setattr(_Handler, "configuration_synchronization", None)
    status, _, body = _request(server, "GET", EXPORT, cookie=cookie)
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert body == b"Source export is unavailable"


def test_export_works_without_oauth_or_spotify_source_service(installation, monkeypatch):
    server, cookie, store, service, _, _ = installation
    before = _files(store.path.parent)
    _forbid_effects(monkeypatch, service)
    monkeypatch.setattr(_Handler, "source_admin_service", None)
    monkeypatch.setattr(_Handler, "spotify_auth", None)
    monkeypatch.setattr(_Handler, "managed_admin_auth", None)
    monkeypatch.setattr(_Handler, "managed_admin_service", None)
    status, _, body = _request(server, "GET", EXPORT, cookie=cookie)
    assert status == HTTPStatus.OK
    assert len(parse_sources(body)) == 14
    assert _files(store.path.parent) == before


def test_quarantined_draft_is_preserved_and_not_included(installation, monkeypatch):
    server, cookie, store, service, _, state = installation
    payload = serialize_managed_state(state)
    payload["schema_version"] = 7
    payload["templates"] = [{"id": "custom_broken", "secret": "private-token-sentinel"}]
    store.path.write_text(json.dumps(payload))
    before = _files(store.path.parent)
    _forbid_effects(monkeypatch, service)
    status, _, body = _request(server, "GET", EXPORT, cookie=cookie)
    assert status == HTTPStatus.OK
    assert len(parse_sources(body)) == 14
    assert b"private-" not in body
    assert _files(store.path.parent) == before


def _installed_definition():
    from news_bulletin_playlist.managed_state import InstalledSource
    from news_bulletin_playlist.models import ExternalReference

    return InstalledSource.from_definition(
        replace(
            BUILTIN_CATALOG.sources[0],
            id=SourceId("installed_portable_ser"),
            external_references=(ExternalReference("spotify", "show", "Z" * 22),),
            countries=(CountryCode("ES"), CountryCode("AR")),
            languages=(LanguageTag("es-ES"), LanguageTag("en")),
        )
    )


def test_v8_installed_capabilities_export_over_authenticated_http(installation, monkeypatch):
    from news_bulletin_playlist.managed_state import source_definition

    server, cookie, store, service, client, state = installation
    source = _installed_definition()
    state = replace(state, schema_version=8, sources=(*state.sources, source))
    store.save(state)
    before = _files(store.path.parent)
    _forbid_effects(monkeypatch, service)
    status, headers, body = _request(
        server, "GET", EXPORT + "?source_id=" + source.id, cookie=cookie
    )
    assert status == HTTPStatus.OK
    assert headers["Cache-Control"] == "no-store"
    assert parse_sources(body)[0].source == source_definition(source)
    assert _files(store.path.parent) == before
    assert client.calls == []


def test_v8_source_http_edit_preserves_languages_countries_and_processing(
    installation, monkeypatch
):
    from test_source_admin_http import _csrf, _update_form

    server, cookie, store, service, client, state = installation
    source = _installed_definition()
    store.save(replace(state, schema_version=8, sources=(*state.sources, source)))
    journal_before = service.provisioning_journal.path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Metadata edit must not call OAuth or Spotify")

    monkeypatch.setattr(service.auth, "get_access_token", forbidden)
    monkeypatch.setattr(service, "client_factory", forbidden)
    status, _, body = _request(
        server, "GET", "/admin/sources/edit?source_id=" + source.id, cookie=cookie
    )
    assert status == HTTPStatus.OK
    assert b"Additional languages: en; countries: ES, AR" in body
    assert_valid_admin_html(body.decode("utf-8"))
    form = {**_update_form(_csrf(body)), "source_id": source.id, "language": "es-MX"}
    assert _request(server, "POST", "/admin/sources/update", cookie=cookie, form=form)[0] == 303
    loaded = store.load()
    revised = next(item for item in loaded.sources if item.id == source.id)
    assert revised.countries == source.countries
    assert revised.languages == (LanguageTag("es-MX"), LanguageTag("en"))
    assert revised.parser_id == source.parser_id
    assert revised.collection_filter_id == source.collection_filter_id
    assert loaded.schema_version == 8
    assert loaded.sources[0] == state.sources[0]
    assert service.provisioning_journal.path.read_bytes() == journal_before
    assert client.calls == []
