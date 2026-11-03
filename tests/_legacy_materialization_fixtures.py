"""Synthetic legacy migration scenario fixtures shared by v11 tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import time

from _installed_fixtures import _installed
from _managed_fixtures import _source
from _playlist_template_fixtures import _draft

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.managed_state import (
    ManagedSourceOverride,
    ManagedState,
    QuarantinedPlaylistTemplate,
    activate_template,
)
from news_bulletin_playlist.models import (
    DurationPolicy,
    DurationPolicyException,
    PublicReleaseMode,
    SourceId,
)

PRODUCTS = (
    "spain_spanish_news",
    "international_english_news",
    "international_polish_news",
    "international_spanish_long_news",
)


def _catalog_with_duration_exception() -> BuiltInCatalog:
    templates = list(BUILTIN_CATALOG.playlists)
    template = next(item for item in templates if str(item.id) == PRODUCTS[0])
    exception = DurationPolicyException("noon", SourceId("ser"), time(12, 0, fold=1), 2700)
    templates[templates.index(template)] = replace(
        template, duration_policy=DurationPolicy(1800, (exception,))
    )
    return BuiltInCatalog(BUILTIN_CATALOG.sources, tuple(templates))


def _legacy_state(version: int, catalog: BuiltInCatalog) -> ManagedState:
    destinations = tuple(
        replace(
            activate_template(catalog.playlist(name), chr(65 + index) * 22),
            enabled=index != 2,
            source_ids=(
                catalog.playlist(name).default_source_ids
                + ((SourceId("custom_" + "A" * 22),) if index == 0 else ())
            ),
            min_duration_seconds=600 if index == 3 else 0,
            max_duration_seconds=2100 if index == 3 else 1800,
            retention_hours=96 if index == 3 else 48,
            max_episodes=80 if index == 0 else 100,
        )
        for index, name in enumerate(PRODUCTS)
    )
    custom_sources = (_source(),)
    if version >= 8:
        custom_sources += (_installed(enabled=False),)
        # A dormant, disabled installed source must stay referenced, not be
        # dropped from the authoritative selection in a legacy migration.
        destinations = tuple(
            replace(p, source_ids=p.source_ids + (SourceId("installed_news"),)) if index == 3 else p
            for index, p in enumerate(destinations)
        )
    if version >= 9:
        destinations = tuple(
            replace(p, portable_id=f"123e4567-e89b-42d3-a456-4266141740{index:02d}")
            for index, p in enumerate(destinations)
        )
    return ManagedState(
        schema_version=version,
        sources=custom_sources,
        playlists=destinations,
        source_overrides=(ManagedSourceOverride(SourceId("rne"), PublicReleaseMode.DISABLED),),
        templates=(_draft(),) if version >= 7 else (),
        quarantined_templates=(
            QuarantinedPlaylistTemplate(0, "invalid_definition", '{"id": "custom_bad"}'),
        )
        if version >= 7
        else (),
    )
