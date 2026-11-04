"""Opt-in bundled playlist examples are local, owner-controlled v11 drafts."""

from __future__ import annotations

from dataclasses import replace
from datetime import time
from pathlib import Path

import pytest
from _cover_fixtures import _artwork

import news_bulletin_playlist.managed_admin as admin_module
import news_bulletin_playlist.playlist_examples as examples_module
from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.custom_cover_assets import CustomCoverAssetStore
from news_bulletin_playlist.managed_admin import ManagedAdminError, ManagedAdminService
from news_bulletin_playlist.managed_state import InstalledSource, ManagedStateError
from news_bulletin_playlist.models import DurationPolicy, DurationPolicyException, SourceId
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME


def _installed_source(name: str, *, enabled: bool = True) -> InstalledSource:
    source = next(item for item in BUILTIN_CATALOG.sources if item.id == name)
    show = source.external_references[0].external_id
    return InstalledSource.from_definition(
        replace(source, id=SourceId(f"custom_{show}"), enabled=enabled)
    )


def _service(root: Path) -> ManagedAdminService:
    store = open_managed_state_store(root)
    artwork = CustomCoverAssetStore(root / "custom-covers")
    return ManagedAdminService(
        store,
        catalog=store.catalog,
        client_factory=lambda _: pytest.fail("Spotify must never be contacted"),
        cover_loader=lambda _: _artwork(),
        cover_installer=artwork.install_canonical,
    )


def test_no_implicit_templates_or_destinations_and_missing_sources_block(tmp_path: Path) -> None:
    service = _service(tmp_path)
    before = (tmp_path / V11_OPERATIONAL_FILENAME).read_bytes()
    snapshot = service.snapshot()
    assert snapshot.managed == snapshot.custom_templates == snapshot.available_templates == ()
    assert len(snapshot.playlist_examples) == 7
    with pytest.raises(ManagedAdminError, match="Import the required source presets"):
        service.create_from_example("international_spanish_news")
    assert (tmp_path / V11_OPERATIONAL_FILENAME).read_bytes() == before
    assert service.snapshot().custom_templates == ()


def test_explicit_example_copies_cover_remaps_show_and_is_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    current = service.store.load()
    installed = _installed_source("cnn")
    service.store.save(replace(current, sources=(installed,)))
    original_example = BUILTIN_CATALOG.playlist("international_spanish_news")
    draft = service.create_from_example(original_example.id)
    assert draft.portable_metadata is None
    assert draft.portable_id is not None
    assert draft.default_source_ids == (installed.id,)
    assert draft.cover_id.startswith("uploaded_")
    assert CustomCoverAssetStore(tmp_path / "custom-covers").load(draft.cover_id) == _artwork()
    assert draft.description == original_example.description
    assert draft.countries == original_example.countries
    assert draft.languages == original_example.languages
    assert draft.retention_hours == original_example.retention_hours
    assert draft.max_episodes == original_example.max_episodes
    assert draft.min_duration_seconds == original_example.duration_policy.default_min_seconds
    assert draft.max_duration_seconds == original_example.duration_policy.default_max_seconds
    assert service.store.load().playlists == ()
    after_first = (tmp_path / V11_OPERATIONAL_FILENAME).read_bytes()
    assert service.create_from_example(original_example.id) == draft
    assert (tmp_path / V11_OPERATIONAL_FILENAME).read_bytes() == after_first
    reopened = _service(tmp_path)
    assert reopened.snapshot().custom_templates == (draft,)
    assert reopened.create_from_example(original_example.id) == draft
    assert (tmp_path / V11_OPERATIONAL_FILENAME).read_bytes() == after_first


def test_disabled_required_source_blocks_without_losing_assignments(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store.save(
        replace(service.store.load(), sources=(_installed_source("cnn", enabled=False),))
    )
    before = service.store.load()
    with pytest.raises(ManagedAdminError, match="Enable required sources"):
        service.create_from_example("international_spanish_news")
    assert service.store.load() == before


def test_nonnative_source_id_and_duration_exceptions_survive_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = BUILTIN_CATALOG.playlist("international_spanish_news")
    exception = DurationPolicyException(
        id="special",
        source_id=SourceId("cnn"),
        edition_local_time=time(8, 15),
        max_seconds=3201,
    )
    adjusted = replace(
        template,
        duration_policy=DurationPolicy(
            default_min_seconds=601, default_max_seconds=2101, exceptions=(exception,)
        ),
    )
    catalog = BuiltInCatalog(BUILTIN_CATALOG.sources, (adjusted,))
    monkeypatch.setattr(admin_module, "BUILTIN_CATALOG", catalog)
    monkeypatch.setattr(examples_module, "BUILTIN_CATALOG", catalog)
    service = _service(tmp_path)
    installed = _installed_source("cnn")
    service.store.save(replace(service.store.load(), sources=(installed,)))
    draft = service.create_from_example(adjusted.id)
    assert draft.min_duration_seconds == 601
    assert draft.max_duration_seconds == 2101
    assert draft.duration_exceptions == (replace(exception, source_id=installed.id),)
    assert draft.portable_metadata is None
    restarted = _service(tmp_path)
    assert restarted.snapshot().custom_templates == (draft,)
    assert restarted.snapshot().custom_templates[0].duration_exceptions == draft.duration_exceptions


def test_stale_v11_cas_cannot_overwrite_a_concurrent_source_install(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store.save(replace(service.store.load(), sources=(_installed_source("cnn"),)))
    other = open_managed_state_store(tmp_path)
    original_install = service.cover_installer
    assert original_install is not None

    def concurrent_install(art: bytes) -> str:
        current = other.load()
        other.save(replace(current, sources=current.sources + (_installed_source("ser"),)))
        return original_install(art)

    service.cover_installer = concurrent_install
    with pytest.raises(ManagedStateError, match="could not be saved"):
        service.create_from_example("international_spanish_news")
    assert not service.store.load().templates
    assert len(service.store.load().sources) == 2
    service.cover_installer = original_install
    draft = service.create_from_example("international_spanish_news")
    assert draft.default_source_ids == (_installed_source("cnn").id,)


def test_admin_html_and_csrf_without_spotify_oauth_or_scheduler(tmp_path: Path) -> None:
    from http import HTTPStatus

    from _html import assert_valid_admin_html
    from _http_handler import captured_response as _response
    from _managed_admin_http_fixtures import _FakeLifecycle, _HandlerHarness

    from news_bulletin_playlist.lan_admin import LanAdminSecurity

    service = _service(tmp_path)
    security = LanAdminSecurity("long-enough-admin-password")
    page = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=_FakeLifecycle(),
        path="/admin/",
        security=security,
    )
    page.spotify_auth = None
    page.engine_lifecycle = None
    page.do_GET()
    assert _response(page).status == HTTPStatus.OK
    html = _response(page).payload.decode()
    assert_valid_admin_html(html)
    assert "Playlist examples" in html
    assert "Download source preset (JSON)" in html
    assert "Create local draft from example" in html
    assert " disabled" in html
    forbidden = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=_FakeLifecycle(),
        path="/admin/playlists/templates/from-example",
        form={"csrf_token": ["incorrect"], "example_id": ["international_spanish_news"]},
        security=security,
    )
    forbidden.engine_lifecycle = None
    forbidden.do_POST()
    assert _response(forbidden).status == HTTPStatus.FORBIDDEN
    service.store.save(replace(service.store.load(), sources=(_installed_source("cnn"),)))
    accepted = _HandlerHarness(
        tmp_path=tmp_path,
        service=service,
        lifecycle=_FakeLifecycle(),
        path="/admin/playlists/templates/from-example",
        form={
            "csrf_token": [security.issue_csrf_token()],
            "example_id": ["international_spanish_news"],
        },
        security=security,
    )
    accepted.engine_lifecycle = None
    accepted.managed_admin_auth = None
    accepted.do_POST()
    assert _response(accepted).status == HTTPStatus.SEE_OTHER
    assert service.snapshot().custom_templates
    assert service.store.load().playlists == ()
