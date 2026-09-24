"""Canonical raw Spotify identifier validation."""

from __future__ import annotations

import re
from typing import Protocol

from news_bulletin_playlist.models import ExternalReference, SourceId

_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")


class SpotifyShowSource(Protocol):
    @property
    def id(self) -> SourceId: ...

    @property
    def external_references(self) -> tuple[ExternalReference, ...]: ...


def is_spotify_id(value: str) -> bool:
    """Return whether value is a canonical 22-character Spotify base62 ID."""
    return _SPOTIFY_ID.fullmatch(value) is not None
