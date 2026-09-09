from __future__ import annotations

from typing import Any


def catalogue_page(
    items: list[object],
    *,
    offset: int = 0,
    limit: int = 50,
    next_url: str | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    """Build the documented Spotify paging envelope for catalogue fakes."""
    if total is None:
        total = offset + len(items) + (1 if next_url is not None else 0)
    return {
        "href": "https://api.spotify.test/catalogue",
        "items": items,
        "limit": limit,
        "next": next_url,
        "offset": offset,
        "previous": None if offset == 0 else "https://api.spotify.test/catalogue/previous",
        "total": total,
    }


def normalize_catalogue_page(
    page: dict[str, Any], *, offset: int, limit: int, total: int | None = None
) -> dict[str, Any]:
    """Upgrade intentionally terse historical fakes to a valid paging envelope."""
    if "limit" in page or "offset" in page or "total" in page:
        return page
    items = page.get("items")
    if not isinstance(items, list):
        return page
    return catalogue_page(
        items,
        offset=offset,
        limit=limit,
        next_url=page.get("next"),
        total=total,
    )
