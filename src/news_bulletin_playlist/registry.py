"""Registry for Spotify-first title parsers and deterministic product filters."""

from __future__ import annotations

from news_bulletin_playlist.providers.base import RecoveryTitleParser, TitleParser
from news_bulletin_playlist.providers.cnn import CnnParser
from news_bulletin_playlist.providers.collection_filters import (
    CollectionFilter,
    UnNewsEsMinutesFilter,
)
from news_bulletin_playlist.providers.cope import CopeParser
from news_bulletin_playlist.providers.ondacero import OndaCeroParser
from news_bulletin_playlist.providers.release_date_title import ReleaseDateTitleParser
from news_bulletin_playlist.providers.rfi import RfiJournalMondeParser
from news_bulletin_playlist.providers.rne import RneParser
from news_bulletin_playlist.providers.ser import SerParser

RECOVERY_TITLE_PARSERS: dict[str, RecoveryTitleParser] = {
    "ser": SerParser(),
    "rne": RneParser(),
    "ondacero": OndaCeroParser(),
    "cnn": CnnParser(),
    "cope": CopeParser(),
    "rfi_journal_monde": RfiJournalMondeParser(),
}
TITLE_PARSERS: dict[str, TitleParser] = {
    **RECOVERY_TITLE_PARSERS,
    "release_date_title": ReleaseDateTitleParser(),
}
COLLECTION_FILTERS: dict[str, CollectionFilter] = {
    "un_news_es_minutes": UnNewsEsMinutesFilter(),
}


def has_title_parser(parser_id: str) -> bool:
    return parser_id in TITLE_PARSERS


def get_title_parser(parser_id: str) -> TitleParser:
    try:
        return TITLE_PARSERS[parser_id]
    except KeyError as exc:
        raise KeyError(f"unknown parser_id: {parser_id}") from exc


def get_recovery_title_parser(parser_id: str) -> RecoveryTitleParser:
    try:
        return RECOVERY_TITLE_PARSERS[parser_id]
    except KeyError as exc:
        raise KeyError(f"parser_id does not support semantic title recovery: {parser_id}") from exc


def has_collection_filter(filter_id: str) -> bool:
    return filter_id in COLLECTION_FILTERS


def get_collection_filter(filter_id: str) -> CollectionFilter:
    try:
        return COLLECTION_FILTERS[filter_id]
    except KeyError as exc:
        raise KeyError(f"unknown collection filter: {filter_id}") from exc
