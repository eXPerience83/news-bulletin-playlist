"""Canonical raw Spotify identifier validation."""

from __future__ import annotations

import re

_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")


def is_spotify_id(value: str) -> bool:
    """Return whether value is a canonical 22-character Spotify base62 ID."""
    return _SPOTIFY_ID.fullmatch(value) is not None
