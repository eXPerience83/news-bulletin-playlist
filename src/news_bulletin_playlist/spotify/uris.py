"""Spotify URI validation shared by catalogue ingestion and playlist writes."""

from __future__ import annotations

import re

_SPOTIFY_EPISODE_URI = re.compile(r"spotify:episode:[A-Za-z0-9]{22}\Z")


def is_spotify_episode_uri(value: str) -> bool:
    """Return whether value is a canonical Spotify episode URI."""
    return _SPOTIFY_EPISODE_URI.fullmatch(value) is not None
