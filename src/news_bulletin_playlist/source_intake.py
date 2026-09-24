"""Bounded, configuration-side-effect-free inspection of Spotify show candidates."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.custom_source_policy import custom_strategy_allowed
from news_bulletin_playlist.managed_state import ManagedState, ManagedStateError, effective_sources
from news_bulletin_playlist.models import ExternalReference, ParserId, SourceId
from news_bulletin_playlist.persistence import PersistenceError
from news_bulletin_playlist.spotify.auth import SpotifyAuthError
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)
from news_bulletin_playlist.spotify.first_catalogue import (
    CatalogueResponseError,
    fetch_source_catalogue,
)
from news_bulletin_playlist.spotify.first_models import TimestampSource, as_utc
from news_bulletin_playlist.spotify.identifiers import is_spotify_id
from news_bulletin_playlist.spotify.public_detector import (
    PublicShowConfigurationError,
    spotify_show_id,
)
from news_bulletin_playlist.spotify_backoff import (
    SpotifyRateLimitBackoffActive,
    SpotifyRateLimitGuardAuth,
    SpotifyRateLimitGuardClient,
    SpotifyRateLimitJournal,
    SpotifyRateLimitStateUnavailable,
    SpotifyRateLimitSuppressed,
)
from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetrics

_LANGUAGE_HINT = re.compile(r"[a-z]{2,3}")
_INSPECTION_RETENTION = timedelta(hours=48)
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
class SourceInspection:
    spotify_show_id: str
    spotify_uri: str
    spotify_url: str
    display_name_hint: str
    language_hints: tuple[str, ...]
    publisher_hint: str | None
    total_episodes_hint: int | None
    parser_id: ParserId
    collection_filter_id: str | None
    catalogue_requests: int
    catalogue_complete: bool
    coverage_proven: bool
    observed_episodes: int
    accepted_episodes: int
    filter_rejected_count: int
    title_timestamp_count: int
    first_seen_timestamp_count: int
    duration_known_count: int
    duration_missing_count: int
    min_duration_seconds: int | None
    max_duration_seconds: int | None
    warnings: tuple[str, ...]


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

    observed = as_utc(now)
    guarded_auth = SpotifyRateLimitGuardAuth(auth, rate_limit, clock=clock)
    factory = client_factory or (lambda token: SpotifyClient(token, metrics=metrics))
    try:
        token = guarded_auth.get_access_token()
        client = SpotifyRateLimitGuardClient(factory(token), rate_limit, clock=clock)
        details = _show_hints(client.show_details(show_id), show_id)
        source = _CandidateCatalogueSource(
            candidate_id,
            (ExternalReference("spotify", "show", show_id),),
            parser_id,
            collection_filter_id,
        )
        snapshot = fetch_source_catalogue(
            client, source, now=observed, cutoff=observed - _INSPECTION_RETENTION
        )
    except SourceIntakeError:
        raise
    except SpotifyRateLimitBackoffActive, SpotifyRateLimitSuppressed:
        raise SourceIntakeError(SourceIntakeFailure.RATE_LIMITED) from None
    except SpotifyRateLimitStateUnavailable, SpotifyTransportError, PersistenceError:
        raise SourceIntakeError(SourceIntakeFailure.UNAVAILABLE) from None
    except SpotifyAuthError:
        raise SourceIntakeError(SourceIntakeFailure.AUTHORIZATION) from None
    except SpotifyApiError as exc:
        if exc.message in {"invalid JSON response", "unexpected non-object JSON response"}:
            failure = SourceIntakeFailure.MALFORMED_RESPONSE
        else:
            failure = {
                400: SourceIntakeFailure.PROVIDER_REJECTION,
                401: SourceIntakeFailure.AUTHORIZATION,
                403: SourceIntakeFailure.FORBIDDEN,
                404: SourceIntakeFailure.PROVIDER_REJECTION,
                429: SourceIntakeFailure.RATE_LIMITED,
            }.get(exc.status, SourceIntakeFailure.UNAVAILABLE)
        raise SourceIntakeError(failure) from None
    except CatalogueResponseError:
        raise SourceIntakeError(SourceIntakeFailure.MALFORMED_RESPONSE) from None

    if not snapshot.coverage_proven:
        raise SourceIntakeError(SourceIntakeFailure.INSUFFICIENT_COVERAGE)
    if not snapshot.episodes:
        raise SourceIntakeError(SourceIntakeFailure.NO_USABLE_EPISODES)
    durations = [episode.duration_seconds for episode in snapshot.episodes]
    known = [seconds for seconds in durations if seconds is not None]
    first_seen = sum(
        episode.timestamp_source is TimestampSource.FIRST_SEEN for episode in snapshot.episodes
    )
    warnings = ("Some episodes have no duration metadata.",) if len(known) != len(durations) else ()
    return SourceInspection(
        spotify_show_id=show_id,
        spotify_uri=f"spotify:show:{show_id}",
        spotify_url=f"https://open.spotify.com/show/{show_id}",
        display_name_hint=details[0],
        language_hints=details[1],
        publisher_hint=details[2],
        total_episodes_hint=details[3],
        parser_id=parser_id,
        collection_filter_id=collection_filter_id,
        catalogue_requests=snapshot.request_count,
        catalogue_complete=snapshot.complete,
        coverage_proven=snapshot.coverage_proven,
        observed_episodes=len(snapshot.api_ids),
        accepted_episodes=len(snapshot.episodes),
        filter_rejected_count=snapshot.filter_rejected_count,
        title_timestamp_count=len(snapshot.episodes) - first_seen,
        first_seen_timestamp_count=first_seen,
        duration_known_count=len(known),
        duration_missing_count=len(durations) - len(known),
        min_duration_seconds=min(known) if known else None,
        max_duration_seconds=max(known) if known else None,
        warnings=warnings,
    )


def _show_hints(
    payload: object, show_id: str
) -> tuple[str, tuple[str, ...], str | None, int | None]:
    if not isinstance(payload, dict):
        raise SourceIntakeError(SourceIntakeFailure.MALFORMED_RESPONSE)
    if (
        payload.get("id") != show_id
        or payload.get("type") != "show"
        or payload.get("uri") != f"spotify:show:{show_id}"
    ):
        raise SourceIntakeError(SourceIntakeFailure.MALFORMED_RESPONSE)
    name = payload.get("name")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 120
        or not name.strip()
        or not name.isprintable()
    ):
        raise SourceIntakeError(SourceIntakeFailure.MALFORMED_RESPONSE)
    languages = payload.get("languages", [])
    if (
        not isinstance(languages, list)
        or len(languages) > 16
        or any(
            not isinstance(item, str) or not _LANGUAGE_HINT.fullmatch(item) for item in languages
        )
    ):
        raise SourceIntakeError(SourceIntakeFailure.MALFORMED_RESPONSE)
    publisher = payload.get("publisher")
    if publisher is not None and (
        not isinstance(publisher, str) or len(publisher) > 120 or not publisher.isprintable()
    ):
        raise SourceIntakeError(SourceIntakeFailure.MALFORMED_RESPONSE)
    total = payload.get("total_episodes")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        raise SourceIntakeError(SourceIntakeFailure.MALFORMED_RESPONSE)
    return name.strip(), tuple(languages), publisher, total
