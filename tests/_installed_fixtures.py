"""Reusable installed-source and mixed managed-state fixtures for test isolation."""

from __future__ import annotations

import json
from dataclasses import replace

from _managed_fixtures import _source, _state
from _playlist_template_fixtures import _draft

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedSourceOverride,
    QuarantinedPlaylistTemplate,
)
from news_bulletin_playlist.models import (
    CountryCode,
    ExternalReference,
    LanguageTag,
    PublicReleaseMode,
    SourceId,
)

EMPTY = BuiltInCatalog((), ())


def _installed(**overrides):
    definition = replace(
        BUILTIN_CATALOG.sources[0],
        id=SourceId("installed_news"),
        external_references=(ExternalReference("spotify", "show", "Z" * 22),),
        countries=(CountryCode("ES"), CountryCode("AR")),
        languages=(LanguageTag("es-ES"), LanguageTag("es-419"), LanguageTag("en")),
    )
    return InstalledSource.from_definition(replace(definition, **overrides))


def _rich_state():
    source = _installed(enabled=False)
    original = _state()
    playlist = replace(
        original.playlists[0], source_ids=(*original.playlists[0].source_ids, source.id)
    )
    draft = _draft(default_source_ids=(source.id, _source().id), cover_id="uploaded_" + "a" * 64)
    return replace(
        original,
        schema_version=8,
        sources=(*original.sources, source),
        playlists=(playlist,),
        source_overrides=(ManagedSourceOverride(SourceId("rne"), PublicReleaseMode.DISABLED),),
        templates=(draft,),
        quarantined_templates=(
            QuarantinedPlaylistTemplate(4, "invalid_definition", json.dumps({"id": "damaged"})),
        ),
    )
