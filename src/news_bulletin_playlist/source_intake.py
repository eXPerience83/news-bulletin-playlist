"""Bounded, configuration-side-effect-free inspection of Spotify show candidates."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.custom_source_policy import custom_strategy_allowed
from news_bulletin_playlist.managed_state import ManagedState, ManagedStateError, effective_sources
from news_bulletin_playlist.models import ExternalReference, ParserId, SourceId
from news_bulletin_playlist.source_inspection import (
    InspectionFailure,
    SourceInspection,
    SourceInspectionError,
    inspect_source,
)
from news_bulletin_playlist.spotify.identifiers import is_spotify_id
from news_bulletin_playlist.spotify.public_detector import (
    PublicShowClient,
    PublicShowConfigurationError,
    spotify_show_id,
)
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal
from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetrics

_DEFAULT_PARSER = ParserId("release_date_title")
_MAX_LOCATOR_LENGTH = 512


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SourceIntakeFailure(StrEnum):
    INVALID_LOCATOR = "invalid_locator"
    DUPLICATE_SHOW = "duplicate_show"
    AUTHORIZATION = "authorization"
    FORBIDDEN = "forbidden"
    PROVIDER_REJECTION = "provider_rejection"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    NO_USABLE_EPISODES = "no_usable_episodes"
    STRATEGY = "strategy"
    CONFIGURATION = "configuration"


_MESSAGES = {
    SourceIntakeFailure.INVALID_LOCATOR: "Enter a canonical Spotify show ID, URI or URL.",
    SourceIntakeFailure.DUPLICATE_SHOW: "This Spotify show is already configured.",
    SourceIntakeFailure.AUTHORIZATION: "Reconnect Spotify authorization and try again.",
    SourceIntakeFailure.FORBIDDEN: "Spotify denied access to this show; check account access.",
    SourceIntakeFailure.PROVIDER_REJECTION: (
        "Spotify rejected this show; verify the ID and account market."
    ),
    SourceIntakeFailure.RATE_LIMITED: "Spotify rate-limit cooldown is active; try again later.",
    SourceIntakeFailure.UNAVAILABLE: "Spotify is temporarily unavailable; try again later.",
    SourceIntakeFailure.MALFORMED_RESPONSE: "Spotify returned invalid show or catalogue data.",
    SourceIntakeFailure.INSUFFICIENT_COVERAGE: "Recent catalogue coverage could not be proven.",
    SourceIntakeFailure.NO_USABLE_EPISODES: (
        "No usable episodes were found for this show and strategy."
    ),
    SourceIntakeFailure.STRATEGY: "This parser or filter is not supported for custom sources.",
    SourceIntakeFailure.CONFIGURATION: "Configured sources are invalid; review managed state.",
}


class SourceIntakeError(ValueError):
    def __init__(self, failure: SourceIntakeFailure) -> None:
        self.failure = failure
        super().__init__(_MESSAGES[failure])


@dataclass(frozen=True, slots=True)
class _CandidateCatalogueSource:
    id: SourceId
    external_references: tuple[ExternalReference, ...]
    parser_id: ParserId
    collection_filter_id: str | None


class _Auth(Protocol):
    def get_access_token(self, *, now: datetime | None = None) -> str: ...


def normalize_spotify_show_locator(locator: str) -> str:
    """Extract an ID without ever resolving or contacting the supplied locator."""
    if not isinstance(locator, str) or len(locator) > _MAX_LOCATOR_LENGTH:
        raise SourceIntakeError(SourceIntakeFailure.INVALID_LOCATOR)
    value = locator.strip()
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise SourceIntakeError(SourceIntakeFailure.INVALID_LOCATOR)
    if is_spotify_id(value):
        return value
    if value.startswith("spotify:show:"):
        show_id = value.removeprefix("spotify:show:")
        if is_spotify_id(show_id):
            return show_id
        raise SourceIntakeError(SourceIntakeFailure.INVALID_LOCATOR)
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.netloc != "open.spotify.com"
            or url.hostname != "open.spotify.com"
            or url.username is not None
            or url.password is not None
            or url.port is not None
            or "#" in value
            or ("?" in value and not url.query)
        ):
            raise ValueError("unsupported locator")
        parts = url.path.split("/")
        if len(parts) != 3 or parts[:2] != ["", "show"] or not is_spotify_id(parts[2]):
            raise ValueError("unsupported show path")
        if url.query and not re.fullmatch(r"si=[A-Za-z0-9_-]{1,128}", url.query):
            raise ValueError("unsupported query")
        return parts[2]
    except ValueError as exc:
        raise SourceIntakeError(SourceIntakeFailure.INVALID_LOCATOR) from exc


def inspect_spotify_source(
    locator: str,
    *,
    catalog: BuiltInCatalog,
    state: ManagedState,
    auth: _Auth,
    rate_limit: SpotifyRateLimitJournal,
    now: datetime,
    parser_id: ParserId = _DEFAULT_PARSER,
    collection_filter_id: str | None = None,
    client_factory: Callable[[str], Any] | None = None,
    public_client: PublicShowClient | None = None,
    observe_public: bool = False,
    metrics: SpotifyRequestMetrics | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> SourceInspection:
    """Validate one candidate with known Spotify endpoints and no source-state writes."""
    show_id = normalize_spotify_show_locator(locator)
    if not custom_strategy_allowed(str(parser_id), collection_filter_id):
        raise SourceIntakeError(SourceIntakeFailure.STRATEGY)
    try:
        sources = effective_sources(catalog, state)
        candidate_id = SourceId(f"custom_{show_id}")
        if any(
            source.id == candidate_id or spotify_show_id(source) == show_id for source in sources
        ):
            raise SourceIntakeError(SourceIntakeFailure.DUPLICATE_SHOW)
    except (ManagedStateError, PublicShowConfigurationError) as exc:
        raise SourceIntakeError(SourceIntakeFailure.CONFIGURATION) from exc

    try:
        source = _CandidateCatalogueSource(
            candidate_id,
            (ExternalReference("spotify", "show", show_id),),
            parser_id,
            collection_filter_id,
        )
        report = inspect_source(
            source,
            ownership="candidate",
            timezone=None,
            auth=auth,
            rate_limit=rate_limit,
            now=now,
            client_factory=client_factory,
            public_client=public_client,
            observe_public=observe_public,
            metrics=metrics,
            clock=clock,
        )
    except SourceInspectionError as exc:
        mapping = {
            InspectionFailure.AUTHORIZATION: SourceIntakeFailure.AUTHORIZATION,
            InspectionFailure.FORBIDDEN: SourceIntakeFailure.FORBIDDEN,
            InspectionFailure.PROVIDER_REJECTION: SourceIntakeFailure.PROVIDER_REJECTION,
            InspectionFailure.RATE_LIMITED: SourceIntakeFailure.RATE_LIMITED,
            InspectionFailure.UNAVAILABLE: SourceIntakeFailure.UNAVAILABLE,
            InspectionFailure.MALFORMED_RESPONSE: SourceIntakeFailure.MALFORMED_RESPONSE,
            InspectionFailure.CONFIGURATION: SourceIntakeFailure.CONFIGURATION,
        }
        raise SourceIntakeError(mapping[exc.failure]) from None

    if not report.coverage_proven:
        raise SourceIntakeError(SourceIntakeFailure.INSUFFICIENT_COVERAGE)
    if not report.accepted_episodes:
        raise SourceIntakeError(SourceIntakeFailure.NO_USABLE_EPISODES)
    return report
