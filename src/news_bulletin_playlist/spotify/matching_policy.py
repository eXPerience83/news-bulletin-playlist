"""Source-scoped Spotify matching policies backed by production evidence."""

from __future__ import annotations

from news_bulletin_playlist.models import SourceDefinition

# Duration is not a globally trustworthy identity signal. Only an exact, measured source/show
# contract may opt in, and the matcher uses the value solely to disambiguate candidates that
# already passed title/time/date semantics. If the parser or show reference changes, the policy
# fails closed until that new contract is measured independently.
_DURATION_DISAMBIGUATION_TOLERANCE_SECONDS: dict[
    tuple[str, str, tuple[str, ...]], int
] = {
    ("ondacero", "ondacero", ("0tjEexypyczHXW9vE3SU3P",)): 1,
}


def duration_disambiguation_tolerance_seconds(source: SourceDefinition) -> int | None:
    """Return a conservative per-source/show duration tolerance, or disable the policy."""
    spotify_show_ids = tuple(
        sorted(
            reference.external_id.strip()
            for reference in source.external_references
            if reference.system.casefold() == "spotify"
            and reference.resource_type.casefold() == "show"
            and reference.external_id.strip()
        )
    )
    return _DURATION_DISAMBIGUATION_TOLERANCE_SECONDS.get(
        (str(source.id), str(source.parser_id), spotify_show_ids)
    )
