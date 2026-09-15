"""Source-scoped Spotify matching policies backed by production evidence."""

from __future__ import annotations

from news_bulletin_playlist.models import SourceDefinition

# Duration is not a globally trustworthy identity signal. Only sources with measured,
# stable RSS↔Spotify duration agreement may opt in here, and the matcher uses the value
# solely to disambiguate candidates that already passed title/time/date semantics.
_DURATION_DISAMBIGUATION_TOLERANCE_SECONDS: dict[str, int] = {
    "ondacero": 1,
}


def duration_disambiguation_tolerance_seconds(source: SourceDefinition) -> int | None:
    """Return a conservative per-source duration tolerance, or disable the policy."""
    return _DURATION_DISAMBIGUATION_TOLERANCE_SECONDS.get(str(source.id))
