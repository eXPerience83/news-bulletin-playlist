"""Explicit strategies supported by installation-owned Spotify sources."""

from __future__ import annotations

from news_bulletin_playlist.registry import has_collection_filter, has_title_parser

CUSTOM_SOURCE_PARSERS = frozenset({"release_date_title"})
CUSTOM_SOURCE_FILTERS: frozenset[str] = frozenset()


def custom_strategy_allowed(parser_id: str, filter_id: str | None) -> bool:
    return (
        parser_id in CUSTOM_SOURCE_PARSERS
        and has_title_parser(parser_id)
        and (
            filter_id is None
            or (
                isinstance(filter_id, str)
                and filter_id in CUSTOM_SOURCE_FILTERS
                and has_collection_filter(filter_id)
            )
        )
    )
