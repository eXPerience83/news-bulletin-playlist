"""Reusable synthetic source and managed-state factories for test scenarios."""

from __future__ import annotations

from dataclasses import replace

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_state import (
    ManagedSource,
    ManagedState,
    activate_template,
)
from news_bulletin_playlist.models import (
    CountryCode,
    EditorialScope,
    LanguageTag,
    ParserId,
    SourceId,
    SourceOrigin,
)

SHOW_A = "A" * 22


def _source(show_id: str = SHOW_A, *, enabled: bool = True) -> ManagedSource:
    return ManagedSource(
        id=SourceId(f"custom_{show_id}"),
        spotify_show_id=show_id,
        display_name="Custom News",
        origin=SourceOrigin.country(CountryCode("ES")),
        language=LanguageTag("es-ES"),
        editorial_scope=EditorialScope.INTERNATIONAL,
        timezone="Europe/Madrid",
        parser_id=ParserId("release_date_title"),
        collection_filter_id=None,
        enabled=enabled,
    )


def _state(source: ManagedSource | None = None) -> ManagedState:
    selected = _source() if source is None else source
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    playlist = replace(
        activate_template(template, "destination"),
        source_ids=(selected.id,),
    )
    return ManagedState(sources=(selected,), playlists=(playlist,))
