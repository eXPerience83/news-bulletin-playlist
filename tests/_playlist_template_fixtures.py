"""Synthetic local playlist template factory for independent test modules."""

from __future__ import annotations

from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.models import CountryCode, LanguageTag, PlaylistId, SourceId


def _draft(**overrides: object) -> LocalPlaylistTemplate:
    values = dict(
        id=PlaylistId("custom_world_briefs"),
        display_name="World briefs",
        description="A personal selection of brief news episodes.",
        default_source_ids=(SourceId("cnn"), SourceId("un_news_en")),
        cover_id="international_english_news",
        countries=(CountryCode("US"),),
        languages=(LanguageTag("en"),),
        retention_hours=72,
        max_episodes=50,
        min_duration_seconds=120,
        max_duration_seconds=900,
    )
    values.update(overrides)
    return LocalPlaylistTemplate(**values)
