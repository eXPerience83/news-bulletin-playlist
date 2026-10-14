"""Validated installation-owned playlist-template definitions (no provider I/O).

This pure domain layer deliberately does not persist or activate a template.  A future
managed-state schema will own storage; Admin will own editing and provisioning.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.custom_cover_assets import is_uploaded_cover_id
from news_bulletin_playlist.managed_duration import validate_duration_range
from news_bulletin_playlist.models import (
    CountryCode,
    DurationPolicy,
    LanguageTag,
    OrderingPolicy,
    PlaylistId,
    SourceDefinition,
    SourceId,
)

# An explicit namespace makes accidental shadowing of built-in identifiers less likely.
_LOCAL_ID = re.compile(r"custom_[a-z0-9][a-z0-9_]{0,55}\Z")
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-(?:[A-Z]{2}|[0-9]{3}))?\Z")
_COUNTRY = re.compile(r"[A-Z]{2}\Z")
MAX_LOCAL_TEMPLATE_NAME = 100
MAX_LOCAL_TEMPLATE_DESCRIPTION = 200
MAX_LOCAL_TEMPLATE_SOURCES = 50
MAX_LOCAL_TEMPLATE_LANGUAGES = 10
MAX_LOCAL_TEMPLATE_COUNTRIES = 10
MAX_LOCAL_TEMPLATE_RETENTION_HOURS = 24 * 30
MAX_LOCAL_TEMPLATE_EPISODES = 100
# Dedicated, locale-neutral default artwork reserved for installation-owned playlists.
CUSTOM_DEFAULT_COVER_ID = "custom_default"
MAX_LOCAL_TEMPLATE_DURATION_SECONDS = 24 * 60 * 60


class LocalPlaylistTemplateError(ValueError):
    """Safe validation failure for an installation-owned playlist definition."""


@dataclass(frozen=True, slots=True)
class LocalPlaylistTemplate:
    """Local draft metadata; not a managed Spotify destination or a built-in."""

    id: PlaylistId
    display_name: str
    description: str
    default_source_ids: tuple[SourceId, ...]
    cover_id: str
    countries: tuple[CountryCode, ...] = ()
    languages: tuple[LanguageTag, ...] = ()
    retention_hours: int = 48
    max_episodes: int = 100
    min_duration_seconds: int = 0
    max_duration_seconds: int = 1800
    ordering: OrderingPolicy = OrderingPolicy.EDITION_AT_DESC
    hidden: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or _LOCAL_ID.fullmatch(self.id) is None:
            raise LocalPlaylistTemplateError("invalid custom playlist template id")
        if (
            not isinstance(self.display_name, str)
            or not self.display_name.strip()
            or self.display_name != self.display_name.strip()
            or len(self.display_name) > MAX_LOCAL_TEMPLATE_NAME
        ):
            raise LocalPlaylistTemplateError("invalid custom playlist name")
        if (
            not isinstance(self.description, str)
            or len(self.description) > MAX_LOCAL_TEMPLATE_DESCRIPTION
        ):
            raise LocalPlaylistTemplateError("invalid custom playlist description")
        if (
            not isinstance(self.default_source_ids, tuple)
            or not 1 <= len(self.default_source_ids) <= MAX_LOCAL_TEMPLATE_SOURCES
            or any(
                not isinstance(value, str) or not value.strip() or value != value.strip()
                for value in self.default_source_ids
            )
            or len(self.default_source_ids) != len(set(self.default_source_ids))
        ):
            raise LocalPlaylistTemplateError("invalid custom playlist sources")
        if not isinstance(self.cover_id, str) or not self.cover_id.strip():
            raise LocalPlaylistTemplateError("invalid custom playlist cover")
        if (
            not isinstance(self.countries, tuple)
            or len(self.countries) > MAX_LOCAL_TEMPLATE_COUNTRIES
            or any(
                not isinstance(country, str) or _COUNTRY.fullmatch(country) is None
                for country in self.countries
            )
            or len(self.countries) != len(set(self.countries))
        ):
            raise LocalPlaylistTemplateError("invalid custom playlist countries")
        if (
            not isinstance(self.languages, tuple)
            or len(self.languages) > MAX_LOCAL_TEMPLATE_LANGUAGES
            or any(
                not isinstance(language, str) or _LANGUAGE.fullmatch(language) is None
                for language in self.languages
            )
            or len(self.languages) != len(set(self.languages))
        ):
            raise LocalPlaylistTemplateError("invalid custom playlist languages")
        if (
            type(self.retention_hours) is not int
            or not 1 <= self.retention_hours <= MAX_LOCAL_TEMPLATE_RETENTION_HOURS
        ):
            raise LocalPlaylistTemplateError("invalid custom playlist retention")
        if (
            type(self.max_episodes) is not int
            or not 1 <= self.max_episodes <= MAX_LOCAL_TEMPLATE_EPISODES
        ):
            raise LocalPlaylistTemplateError("invalid custom playlist episode limit")
        if (
            type(self.min_duration_seconds) is not int
            or type(self.max_duration_seconds) is not int
            or not 0 <= self.min_duration_seconds <= MAX_LOCAL_TEMPLATE_DURATION_SECONDS
            or not 0 <= self.max_duration_seconds <= MAX_LOCAL_TEMPLATE_DURATION_SECONDS
        ):
            raise LocalPlaylistTemplateError("invalid custom playlist duration")
        try:
            validate_duration_range(self.min_duration_seconds, self.max_duration_seconds)
        except ValueError as exc:
            raise LocalPlaylistTemplateError("invalid custom playlist duration") from exc
        if self.ordering is not OrderingPolicy.EDITION_AT_DESC:
            raise LocalPlaylistTemplateError("unsupported custom playlist ordering")
        if type(self.hidden) is not bool:
            raise LocalPlaylistTemplateError("invalid custom playlist visibility")

    def as_playlist_template(self) -> PlaylistTemplate:
        """Reuse the exact immutable built-in template contract, without mutating it."""
        return PlaylistTemplate(
            id=self.id,
            display_name=self.display_name,
            description=self.description,
            countries=self.countries,
            languages=self.languages,
            default_source_ids=self.default_source_ids,
            cover_id=self.cover_id,
            retention_hours=self.retention_hours,
            max_episodes=self.max_episodes,
            ordering=self.ordering,
            duration_policy=DurationPolicy(
                default_min_seconds=self.min_duration_seconds,
                default_max_seconds=self.max_duration_seconds,
            ),
        )


def effective_playlist_templates(
    catalog: BuiltInCatalog,
    custom_templates: Iterable[LocalPlaylistTemplate],
    *,
    sources: Iterable[SourceDefinition],
) -> tuple[PlaylistTemplate, ...]:
    """Compose immutable built-ins and valid local templates in a stable order.

    Hidden templates are retained because an already-activated managed destination
    may reference one; presentation/activation must filter hidden drafts separately.
    Disabled sources can remain referenced in a draft after a later source-disable;
    the existing activation contract still requires an enabled selection.
    """
    local = tuple(custom_templates)
    built_in_ids = {template.id for template in catalog.playlists}
    allowed_covers = {template.cover_id for template in catalog.playlists}
    allowed_covers.add(CUSTOM_DEFAULT_COVER_ID)
    source_ids = {source.id for source in sources}
    seen: set[PlaylistId] = set()
    output: list[PlaylistTemplate] = []
    for item in local:
        if not isinstance(item, LocalPlaylistTemplate):
            raise LocalPlaylistTemplateError("invalid custom playlist definition")
        # Dataclass fields can be modified by replacement or untrusted decode.
        item.__post_init__()
        if item.id in built_in_ids or item.id in seen:
            raise LocalPlaylistTemplateError("duplicate or built-in playlist template id")
        if item.cover_id not in allowed_covers and not is_uploaded_cover_id(item.cover_id):
            raise LocalPlaylistTemplateError("unknown bundled playlist cover")
        if any(source_id not in source_ids for source_id in item.default_source_ids):
            raise LocalPlaylistTemplateError("custom playlist references unknown source")
        seen.add(item.id)
        output.append(item.as_playlist_template())
    output.sort(key=lambda template: str(template.id))
    return catalog.playlists + tuple(output)
