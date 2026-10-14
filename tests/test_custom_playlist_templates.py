"""Pure-domain regression checks for upcoming installation-owned playlist templates."""

from dataclasses import replace

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.custom_playlist_templates import (
    LocalPlaylistTemplate,
    LocalPlaylistTemplateError,
    effective_playlist_templates,
)
from news_bulletin_playlist.models import (
    CountryCode,
    LanguageTag,
    PlaylistId,
    SourceId,
)


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


def test_custom_draft_converts_to_reusable_catalog_template() -> None:
    draft = _draft()
    catalog = effective_playlist_templates(
        BUILTIN_CATALOG, (draft,), sources=BUILTIN_CATALOG.sources
    )
    assert catalog[:-1] == BUILTIN_CATALOG.playlists
    converted = catalog[-1]
    assert converted.id == draft.id
    assert converted.default_source_ids == draft.default_source_ids
    assert converted.retention_hours == 72
    assert converted.max_episodes == 50
    assert converted.duration_policy.default_min_seconds == 120
    assert converted.duration_policy.default_max_seconds == 900
    assert converted.languages == (LanguageTag("en"),)
    assert BUILTIN_CATALOG.playlists[-1] != converted


def test_hidden_template_remains_resolvable_for_existing_managed_references() -> None:
    hidden = _draft(hidden=True)
    catalog = effective_playlist_templates(
        BUILTIN_CATALOG, (hidden,), sources=BUILTIN_CATALOG.sources
    )
    assert catalog[-1].id == hidden.id
    assert hidden.hidden is True


def test_custom_drafts_are_ordered_deterministically_without_reordering_builtins() -> None:
    z = _draft(id=PlaylistId("custom_z"))
    a = _draft(id=PlaylistId("custom_a"))
    result = effective_playlist_templates(BUILTIN_CATALOG, (z, a), sources=BUILTIN_CATALOG.sources)
    assert [str(item.id) for item in result[-2:]] == ["custom_a", "custom_z"]


@pytest.mark.parametrize(
    "bad",
    [
        {"id": PlaylistId("international_english_news")},
        {"id": PlaylistId("CUSTOM_bad")},
        {"display_name": ""},
        {"display_name": " x "},
        {"display_name": "x" * 101},
        {"description": "x" * 201},
        {"default_source_ids": ()},
        {"default_source_ids": (SourceId("cnn"), SourceId("cnn"))},
        {"default_source_ids": (" cnn",)},
        {"cover_id": ""},
        {"countries": ("us",)},
        {"languages": ("EN",)},
        {"retention_hours": 0},
        {"retention_hours": True},
        {"max_episodes": 101},
        {"max_episodes": True},
        {"min_duration_seconds": 901},
        {"max_duration_seconds": 86401},
        {"hidden": "yes"},
    ],
)
def test_rejects_invalid_local_drafts(bad: dict[str, object]) -> None:
    with pytest.raises(LocalPlaylistTemplateError):
        _draft(**bad)


def test_rejects_colliding_id_or_cover_or_deleted_source() -> None:
    draft = _draft()
    with pytest.raises(LocalPlaylistTemplateError, match="duplicate"):
        effective_playlist_templates(
            BUILTIN_CATALOG, (draft, draft), sources=BUILTIN_CATALOG.sources
        )
    with pytest.raises(LocalPlaylistTemplateError, match="cover"):
        effective_playlist_templates(
            BUILTIN_CATALOG,
            (replace(draft, cover_id="nonexistent"),),
            sources=BUILTIN_CATALOG.sources,
        )
    with pytest.raises(LocalPlaylistTemplateError, match="source"):
        effective_playlist_templates(
            BUILTIN_CATALOG,
            (_draft(default_source_ids=(SourceId("not_installed"),)),),
            sources=BUILTIN_CATALOG.sources,
        )


def test_draft_with_previously_disabled_source_retains_reference() -> None:
    disabled = replace(BUILTIN_CATALOG.sources[0], enabled=False)
    draft = _draft(default_source_ids=(disabled.id,))
    catalog = effective_playlist_templates(
        BUILTIN_CATALOG, (draft,), sources=(disabled,) + BUILTIN_CATALOG.sources[1:]
    )
    assert catalog[-1].default_source_ids == (disabled.id,)
