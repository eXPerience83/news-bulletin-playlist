"""Managed ZIP v1 must fail closed on template state it cannot carry for future edits."""

from __future__ import annotations

from dataclasses import replace
from datetime import time

import pytest
from _portable_playlist_fixtures import UUID, _service

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.managed_state import ManagedState, activate_template
from news_bulletin_playlist.models import DurationPolicy, DurationPolicyException
from news_bulletin_playlist.portable_playlists import PlaylistExportError


def _prepared(tmp_path, template, *, playlist=None):
    catalog = BuiltInCatalog(BUILTIN_CATALOG.sources, (template,))
    if playlist is None:
        playlist = replace(activate_template(template, "PRIVATE"), portable_id=UUID)
    service = _service(
        tmp_path, ManagedState(schema_version=9, playlists=(playlist,)), catalog=catalog
    )
    return service, playlist


def test_inactive_template_exception_does_not_disappear_from_export(tmp_path):
    template = BUILTIN_CATALOG.playlists[0]
    dormant = DurationPolicyException(
        "newly-relevant-after-edit",
        template.default_source_ids[0],
        time(12, 30, 5, 123456, fold=1),
        2700,
    )
    template = replace(
        template,
        duration_policy=DurationPolicy(1800, (dormant,)),
    )
    playlist = replace(
        activate_template(template, "PRIVATE"),
        portable_id=UUID,
        max_duration_seconds=3000,
    )
    service, _ = _prepared(tmp_path, template, playlist=playlist)
    before = service.store.path.read_bytes()
    with pytest.raises(PlaylistExportError, match="future-use duration exceptions"):
        service.export("managed", str(playlist.id))
    assert service.store.path.read_bytes() == before

    # The exact same template is representable while the exception is active.
    active = replace(playlist, max_duration_seconds=1800)
    service.store.save(replace(service.store.load_readonly(), playlists=(active,)))
    _id, document = service.export("managed", str(active.id))
    from news_bulletin_playlist.playlist_bundle import inspect_playlist_bundle

    restored = inspect_playlist_bundle(document)
    assert restored.exceptions == (dormant,)
    assert restored.exceptions[0].edition_local_time.fold == 1
    assert restored.exceptions[0].edition_local_time.microsecond == 123456


def test_unassigned_template_source_does_not_disappear_from_managed_zip(tmp_path):
    template = BUILTIN_CATALOG.playlists[0]
    playlist = replace(
        activate_template(template, "PRIVATE"),
        portable_id=UUID,
        source_ids=(template.default_source_ids[0],),
    )
    service, _ = _prepared(tmp_path, template, playlist=playlist)
    with pytest.raises(PlaylistExportError, match="unassigned sources"):
        service.export("managed", str(playlist.id))


def test_reordered_template_sources_are_not_exported_as_original_default_order(tmp_path):
    template = BUILTIN_CATALOG.playlists[0]
    playlist = replace(
        activate_template(template, "PRIVATE"),
        portable_id=UUID,
        source_ids=tuple(reversed(template.default_source_ids)),
    )
    service, _ = _prepared(tmp_path, template, playlist=playlist)
    with pytest.raises(PlaylistExportError, match="source order"):
        service.export("managed", str(playlist.id))


def test_currently_active_exception_can_roundtrip_without_loss(tmp_path):
    template = BUILTIN_CATALOG.playlists[0]
    exception = DurationPolicyException(
        "edition", template.default_source_ids[0], time(8, 20, fold=1), 4200
    )
    template = replace(template, duration_policy=DurationPolicy(1800, (exception,)))
    service, playlist = _prepared(tmp_path, template)
    _, payload = service.export("managed", str(playlist.id))
    from news_bulletin_playlist.playlist_bundle import inspect_playlist_bundle

    assert inspect_playlist_bundle(payload).exceptions == (exception,)
