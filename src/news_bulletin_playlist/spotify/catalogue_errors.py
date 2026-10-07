"""Stable, closed catalogue validation labels shared with Diagnostics."""

from enum import StrEnum


class CatalogueValidationReason(StrEnum):
    """Diagnostic contract: values identify existing guards, never rejected payload values."""

    PAGE_NOT_OBJECT = "page_not_object"
    ITEMS_NOT_LIST = "items_not_list"
    INVALID_LIMIT = "invalid_limit"
    INVALID_OFFSET = "invalid_offset"
    INVALID_TOTAL = "invalid_total"
    TOTAL_CHANGED = "total_changed"
    INVALID_NEXT = "invalid_next"
    NEXT_CONTRADICTION = "next_contradiction"
    IMPOSSIBLE_PAGING = "impossible_paging"
    ITEM_NOT_OBJECT = "item_not_object"
    INVALID_EPISODE_ID = "invalid_episode_id"
    INVALID_EPISODE_URI = "invalid_episode_uri"
    EPISODE_URI_ID_MISMATCH = "episode_uri_id_mismatch"
    INVALID_TITLE = "invalid_title"
    INVALID_RELEASE_DATE = "invalid_release_date"
    INVALID_RELEASE_PRECISION = "invalid_release_precision"
    INVALID_DURATION = "invalid_duration"
    DUPLICATE_EPISODE_ID = "duplicate_episode_id"


class CatalogueItemValueKind(StrEnum):
    """Closed JSON kinds; OTHER covers unexpected non-JSON client/fake values."""

    NULL = "null"
    ARRAY = "array"
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OTHER = "other"
