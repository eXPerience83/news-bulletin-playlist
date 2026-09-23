"""Managed-admin rendering entrypoints and form validation helpers."""

from __future__ import annotations

from collections.abc import Mapping

from news_bulletin_playlist import admin_web
from news_bulletin_playlist.managed_duration import validate_persisted_duration_seconds
from news_bulletin_playlist.models import PlaylistId

render_managed_admin_page = admin_web.render_playlists_page
render_sources_page = admin_web.render_sources_page


def single_form_value(
    form: Mapping[str, list[str]],
    name: str,
    *,
    required: bool = True,
) -> str:
    values = form.get(name, [])
    if not values and not required:
        return ""
    if len(values) != 1:
        raise ValueError(f"Exactly one {name} value is required")
    return values[0]


def max_duration_seconds_from_form(form: Mapping[str, list[str]]) -> int | None:
    values = form.get("max_duration_minutes", [])
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("Exactly one max_duration_minutes value is required")
    raw = values[0].strip()
    remainder_values = form.get("max_duration_seconds_remainder", [])
    if not remainder_values:
        remainder = 0  # Compatibility with existing minute-only POSTs.
    elif len(remainder_values) == 1:
        try:
            remainder = int(remainder_values[0].strip())
        except ValueError as exc:
            raise ValueError("max_duration_seconds_remainder must be an integer") from exc
    else:
        raise ValueError("Exactly one max_duration_seconds_remainder value is required")
    try:
        minutes = int(raw)
    except ValueError as exc:
        raise ValueError("max_duration_minutes must be an integer") from exc
    if minutes < 0 or not 0 <= remainder < 60:
        raise ValueError("maximum duration must be positive with seconds remainder from 0 to 59")
    try:
        return validate_persisted_duration_seconds(minutes * 60 + remainder)
    except ValueError as exc:
        raise ValueError("maximum duration must be positive") from exc


def playlist_id_from_form(form: Mapping[str, list[str]]) -> PlaylistId:
    value = single_form_value(form, "playlist_id").strip()
    if not value:
        raise ValueError("playlist_id must not be empty")
    return PlaylistId(value)
