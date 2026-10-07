"""On-demand, normalized evidence for a Spotify show without source-state writes."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from news_bulletin_playlist import __version__
from news_bulletin_playlist.build_info import build_revision
from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.diagnostics import DiagnosticSeverity, DiagnosticValue
from news_bulletin_playlist.duration_profile import (
    DurationProfile,
    duration_profile,
    profile_as_dict,
)
from news_bulletin_playlist.managed_state import ManagedState, ManagedStateError, effective_sources
from news_bulletin_playlist.models import ParserId, PublicReleaseMode, SourceId, TemporalStrategy
from news_bulletin_playlist.persistence import PersistenceError
from news_bulletin_playlist.providers.title_recovery import TemporalEvidenceKind
from news_bulletin_playlist.registry import has_collection_filter, has_title_parser
from news_bulletin_playlist.spotify.auth import SpotifyAuthError
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)
from news_bulletin_playlist.spotify.first_catalogue import (
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_SIZE,
    CatalogueEpisodeEvidence,
    CatalogueResponseError,
    CatalogueSource,
    apply_public_release_evidence,
    fetch_source_catalogue,
)
from news_bulletin_playlist.spotify.first_models import TimestampSource, as_utc
from news_bulletin_playlist.spotify.public_detector import (
    PublicEpisodeObservation,
    PublicShowClient,
    PublicShowConfigurationError,
    PublicShowDetectorError,
    spotify_show_id,
)
from news_bulletin_playlist.spotify.public_release import (
    PublicReleaseEvidence,
    PublicReleaseOutcome,
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

REPORT_SCHEMA_VERSION = 1
INSPECTION_RETENTION = timedelta(hours=48)
_LANGUAGE_HINT = re.compile(r"[a-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}")
_MAX_REPORT_TEXT = 200
_MAX_DISPLAY_NAME_HINT = 120


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InspectionFailure(StrEnum):
    AUTHORIZATION = "authorization"
    FORBIDDEN = "forbidden"
    PROVIDER_REJECTION = "provider_rejection"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    CONFIGURATION = "configuration"


class InspectionResponseReason(StrEnum):
    SHOW_OBJECT = "show_object"
    SHOW_IDENTITY = "show_identity"
    SHOW_NAME = "show_name"
    SHOW_LANGUAGES = "show_languages"
    SHOW_PUBLISHER = "show_publisher"
    SHOW_TOTAL_EPISODES = "show_total_episodes"
    PROVIDER_JSON = "provider_json"
    CATALOGUE_SHAPE = "catalogue_shape"


_MESSAGES = {
    InspectionFailure.AUTHORIZATION: "Reconnect Spotify authorization and try again.",
    InspectionFailure.FORBIDDEN: "Spotify denied access to this show; check account access.",
    InspectionFailure.PROVIDER_REJECTION: (
        "Spotify rejected this show; verify the ID and account market."
    ),
    InspectionFailure.RATE_LIMITED: "Spotify rate-limit cooldown is active; try again later.",
    InspectionFailure.UNAVAILABLE: "Spotify is temporarily unavailable; try again later.",
    InspectionFailure.MALFORMED_RESPONSE: "Spotify returned invalid show or catalogue data.",
    InspectionFailure.CONFIGURATION: "Configured source or strategy is invalid.",
}


class SourceInspectionError(ValueError):
    def __init__(
        self,
        failure: InspectionFailure,
        response_reason: InspectionResponseReason | None = None,
    ) -> None:
        self.failure = failure
        self.response_reason = response_reason
        super().__init__(_MESSAGES[failure])


class _Auth(Protocol):
    def get_access_token(self, *, now: datetime | None = None) -> str: ...


class _PublicClient(Protocol):
    def observe_episode(
        self, source_id: SourceId, spotify_show_id: str, *, observed_at: datetime
    ) -> PublicEpisodeObservation: ...


@dataclass(frozen=True, slots=True)
class EpisodeInspectionEvidence:
    catalogue_position: int
    spotify_episode_id: str
    spotify_episode_uri: str
    title: str
    title_truncated: bool
    release_date: str
    release_date_precision: str
    duration_seconds: int | None
    collection_filter_outcome: str
    parser_outcome: str
    edition_at: datetime | None
    timestamp_source: TimestampSource | None
    ordering_evidence: str
    warnings: tuple[str, ...]
    temporal_evidence: TemporalEvidenceKind | None = None
    temporal_supporting_rows: int = 0
    public_release_outcome: PublicReleaseOutcome = PublicReleaseOutcome.NO_EVIDENCE


@dataclass(frozen=True, slots=True)
class PublicInspectionEvidence:
    episode_id: str
    episode_uri: str | None
    title: str
    title_truncated: bool
    duration_seconds: int | None
    release_date: str | None
    release_date_truncated: bool
    release_date_shape: str
    detector_schema_version: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SourceInspection:
    schema_version: int
    inspected_at: datetime
    application_version: str
    build_revision: str
    ownership: str
    source_id: SourceId | None
    spotify_show_id: str
    spotify_uri: str
    spotify_url: str
    display_name_hint: str
    language_hints: tuple[str, ...]
    publisher_hint: str | None
    total_episodes_hint: int | None
    parser_id: ParserId
    collection_filter_id: str | None
    timezone: str | None
    retention_hours: int
    cutoff_at: datetime
    page_size: int
    max_pages: int
    catalogue_requests: int
    catalogue_complete: bool
    coverage_proven: bool
    observed_episodes: int
    accepted_episodes: int
    filter_rejected_count: int
    parser_fallback_count: int
    title_timestamp_count: int
    first_seen_timestamp_count: int
    duration_known_count: int
    duration_missing_count: int
    observed_duration_missing_count: int
    min_duration_seconds: int | None
    max_duration_seconds: int | None
    duration_buckets: DurationProfile
    observed_duration_buckets: DurationProfile
    warnings: tuple[str, ...]
    episodes: tuple[EpisodeInspectionEvidence, ...]
    public_status: str
    public_observation: PublicInspectionEvidence | None
    public_release_mode: PublicReleaseMode = PublicReleaseMode.INSTANT
    temporal_strategy: TemporalStrategy = TemporalStrategy.AUTO
    temporal_supporting_rows: int = 0
    temporal_conflicts: int = 0


def _bounded(value: str) -> tuple[str, bool]:
    return value[:_MAX_REPORT_TEXT], len(value) > _MAX_REPORT_TEXT


def _malformed(reason: InspectionResponseReason) -> SourceInspectionError:
    return SourceInspectionError(InspectionFailure.MALFORMED_RESPONSE, reason)


def _show_hints(
    payload: object, show_id: str
) -> tuple[str, tuple[str, ...], str | None, int | None]:
    if not isinstance(payload, dict):
        raise _malformed(InspectionResponseReason.SHOW_OBJECT)
    if payload.get("id") != show_id:
        raise _malformed(InspectionResponseReason.SHOW_IDENTITY)
    show_type = payload.get("type")
    if show_type is not None and show_type != "show":
        raise _malformed(InspectionResponseReason.SHOW_IDENTITY)
    uri = payload.get("uri")
    if uri is not None and uri != f"spotify:show:{show_id}":
        raise _malformed(InspectionResponseReason.SHOW_IDENTITY)

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip() or not name.isprintable():
        raise _malformed(InspectionResponseReason.SHOW_NAME)
    display_name_hint = name.strip()[:_MAX_DISPLAY_NAME_HINT]

    languages = payload.get("languages")
    if languages is None:
        language_hints: tuple[str, ...] = ()
    else:
        if (
            not isinstance(languages, list)
            or len(languages) > 16
            or any(
                not isinstance(item, str) or not _LANGUAGE_HINT.fullmatch(item)
                for item in languages
            )
        ):
            raise _malformed(InspectionResponseReason.SHOW_LANGUAGES)
        language_hints = tuple(languages)

    publisher = payload.get("publisher")
    if publisher is None or publisher == "":
        publisher_hint = None
    elif not isinstance(publisher, str) or not publisher.isprintable():
        raise _malformed(InspectionResponseReason.SHOW_PUBLISHER)
    else:
        publisher_hint, _ = _bounded(publisher)

    total = payload.get("total_episodes")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        raise _malformed(InspectionResponseReason.SHOW_TOTAL_EPISODES)
    return display_name_hint, language_hints, publisher_hint, total


def _emit_response_failure(
    rate_limit: SpotifyRateLimitJournal,
    *,
    source_id: SourceId,
    occurred_at: datetime,
    reason: InspectionResponseReason,
    catalogue_error: CatalogueResponseError | None = None,
) -> None:
    diagnostics = rate_limit.diagnostics
    if diagnostics is None:
        return
    collection_reason = reason in {
        InspectionResponseReason.CATALOGUE_SHAPE,
        InspectionResponseReason.PROVIDER_JSON,
    }
    phase = "collection" if collection_reason else "configuration"
    details: dict[str, DiagnosticValue] = {"failure_class": "response_shape_error", "phase": phase}
    if catalogue_error is not None:
        details["catalogue_reason"] = catalogue_error.reason.value
        details["requested_offset"] = catalogue_error.requested_offset
        if catalogue_error.item_index is not None:
            details["item_index"] = catalogue_error.item_index
        if catalogue_error.expected_total is not None:
            details["expected_total"] = catalogue_error.expected_total
        if catalogue_error.observed_total is not None:
            details["observed_total"] = catalogue_error.observed_total
    diagnostics.emit(
        occurred_at=occurred_at,
        severity=DiagnosticSeverity.WARNING,
        component="source_inspection",
        event_name=f"source_inspection_{reason.value}_invalid",
        source_id=str(source_id),
        details=details,
    )


def _episode_evidence(
    items: tuple[CatalogueEpisodeEvidence, ...],
) -> tuple[EpisodeInspectionEvidence, ...]:
    coarse_counts: dict[str, int] = {}
    for item in items:
        if (
            item.filter_outcome == "accepted"
            and item.edition_at is None
            and item.release_date_precision == "day"
        ):
            coarse_counts[item.release_date] = coarse_counts.get(item.release_date, 0) + 1
    result = []
    for item in items:
        title, truncated = _bounded(item.title)
        ambiguous = (
            item.filter_outcome == "accepted"
            and item.edition_at is None
            and item.release_date_precision == "day"
            and coarse_counts[item.release_date] > 1
        )
        ordering = (
            "not_evaluated"
            if item.filter_outcome == "rejected"
            else "exact"
            if item.edition_at is not None
            else "ambiguous"
            if ambiguous
            else "coarse"
        )
        result.append(
            EpisodeInspectionEvidence(
                item.position,
                item.spotify_episode_id,
                item.spotify_episode_uri,
                title,
                truncated,
                item.release_date,
                item.release_date_precision,
                item.duration_seconds,
                item.filter_outcome,
                item.parser_outcome,
                item.edition_at,
                item.timestamp_source,
                ordering,
                ("same_day_order_ambiguous",) if ambiguous else (),
                item.temporal_evidence,
                item.temporal_supporting_rows,
                item.public_release_outcome,
            )
        )
    return tuple(result)


def _public_evidence(observation: PublicEpisodeObservation) -> PublicInspectionEvidence:
    title, truncated = _bounded(observation.title)
    release_date = observation.release_date_raw
    release_date_truncated = False
    if release_date is not None:
        release_date, release_date_truncated = _bounded(release_date)
    return PublicInspectionEvidence(
        observation.episode_id,
        observation.episode_uri,
        title,
        truncated,
        None if observation.duration_ms is None else round(observation.duration_ms / 1000),
        release_date,
        release_date_truncated,
        observation.release_date_shape.value,
        observation.detector_schema_version,
        observation.observed_at,
    )


def inspect_source(
    source: CatalogueSource,
    *,
    ownership: str,
    timezone: str | None,
    auth: _Auth,
    rate_limit: SpotifyRateLimitJournal,
    now: datetime,
    client_factory: Callable[[str], Any] | None = None,
    public_client: _PublicClient | None = None,
    observe_public: bool = True,
    public_release_evidence: PublicReleaseEvidence | None = None,
    metrics: SpotifyRequestMetrics | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> SourceInspection:
    """Inspect configured or candidate strategy through the one production catalogue path."""
    if (
        ownership not in {"candidate", "custom", "built_in"}
        or not has_title_parser(str(source.parser_id))
        or (
            source.collection_filter_id is not None
            and not has_collection_filter(source.collection_filter_id)
        )
    ):
        raise SourceInspectionError(InspectionFailure.CONFIGURATION)
    try:
        show_id = spotify_show_id(source)
    except PublicShowConfigurationError:
        raise SourceInspectionError(InspectionFailure.CONFIGURATION) from None
    observed = as_utc(now)
    guarded_auth = SpotifyRateLimitGuardAuth(auth, rate_limit, clock=clock)
    factory = client_factory or (lambda token: SpotifyClient(token, metrics=metrics))
    public_status = "not_requested" if not observe_public else "available"
    public_observation = None
    try:
        token = guarded_auth.get_access_token()
        client = SpotifyRateLimitGuardClient(factory(token), rate_limit, clock=clock)
        details = _show_hints(client.show_details(show_id), show_id)
        snapshot = fetch_source_catalogue(
            client,
            source,
            now=observed,
            cutoff=observed - INSPECTION_RETENTION,
            collect_evidence=True,
            public_release_evidence=public_release_evidence,
        )
        if observe_public:
            public_client = public_client or PublicShowClient(metrics=metrics)
            try:
                observation = public_client.observe_episode(
                    source.id, show_id, observed_at=observed
                )
                public_observation = _public_evidence(observation)
                snapshot = apply_public_release_evidence(
                    snapshot, source, PublicReleaseEvidence.from_observation(observation)
                )
            except PublicShowDetectorError, ValueError:
                public_status = "unavailable"
    except SourceInspectionError as exc:
        if exc.failure is InspectionFailure.MALFORMED_RESPONSE and exc.response_reason is not None:
            _emit_response_failure(
                rate_limit,
                source_id=source.id,
                occurred_at=observed,
                reason=exc.response_reason,
            )
        raise
    except SpotifyRateLimitBackoffActive, SpotifyRateLimitSuppressed:
        raise SourceInspectionError(InspectionFailure.RATE_LIMITED) from None
    except SpotifyRateLimitStateUnavailable, SpotifyTransportError, PersistenceError:
        raise SourceInspectionError(InspectionFailure.UNAVAILABLE) from None
    except SpotifyAuthError:
        raise SourceInspectionError(InspectionFailure.AUTHORIZATION) from None
    except SpotifyApiError as exc:
        if exc.message in {"invalid JSON response", "unexpected non-object JSON response"}:
            failure = SourceInspectionError(
                InspectionFailure.MALFORMED_RESPONSE,
                InspectionResponseReason.PROVIDER_JSON,
            )
            _emit_response_failure(
                rate_limit,
                source_id=source.id,
                occurred_at=observed,
                reason=InspectionResponseReason.PROVIDER_JSON,
            )
            raise failure from None
        failure_kind = {
            400: InspectionFailure.PROVIDER_REJECTION,
            401: InspectionFailure.AUTHORIZATION,
            403: InspectionFailure.FORBIDDEN,
            404: InspectionFailure.PROVIDER_REJECTION,
            429: InspectionFailure.RATE_LIMITED,
        }.get(exc.status, InspectionFailure.UNAVAILABLE)
        raise SourceInspectionError(failure_kind) from None
    except CatalogueResponseError as exc:
        _emit_response_failure(
            rate_limit,
            source_id=source.id,
            occurred_at=observed,
            reason=InspectionResponseReason.CATALOGUE_SHAPE,
            catalogue_error=exc,
        )
        raise SourceInspectionError(
            InspectionFailure.MALFORMED_RESPONSE,
            InspectionResponseReason.CATALOGUE_SHAPE,
        ) from None

    evidence = _episode_evidence(snapshot.evidence)
    known = [
        episode.duration_seconds
        for episode in snapshot.episodes
        if episode.duration_seconds is not None
    ]
    observed_known = [
        item.duration_seconds for item in snapshot.evidence if item.duration_seconds is not None
    ]
    first_seen = sum(
        episode.timestamp_source is TimestampSource.FIRST_SEEN for episode in snapshot.episodes
    )
    warnings = []
    if not snapshot.coverage_proven:
        warnings.append("coverage_unproven")
    if not snapshot.episodes:
        warnings.append("no_usable_episodes")
    if len(known) != len(snapshot.episodes):
        warnings.append("duration_missing")
    if any(item.ordering_evidence == "ambiguous" for item in evidence):
        warnings.append("same_day_order_ambiguous")
    if public_status == "unavailable":
        warnings.append("public_observation_unavailable")

    return SourceInspection(
        schema_version=REPORT_SCHEMA_VERSION,
        inspected_at=observed,
        application_version=__version__,
        build_revision=build_revision(),
        ownership=ownership,
        source_id=None if ownership == "candidate" else source.id,
        spotify_show_id=show_id,
        spotify_uri=f"spotify:show:{show_id}",
        spotify_url=f"https://open.spotify.com/show/{show_id}",
        display_name_hint=details[0],
        language_hints=details[1],
        publisher_hint=details[2],
        total_episodes_hint=details[3],
        parser_id=source.parser_id,
        collection_filter_id=source.collection_filter_id,
        timezone=timezone,
        retention_hours=48,
        cutoff_at=observed - INSPECTION_RETENTION,
        page_size=DEFAULT_PAGE_SIZE,
        max_pages=DEFAULT_MAX_PAGES,
        catalogue_requests=snapshot.request_count,
        catalogue_complete=snapshot.complete,
        coverage_proven=snapshot.coverage_proven,
        observed_episodes=len(snapshot.api_ids),
        accepted_episodes=len(snapshot.episodes),
        filter_rejected_count=snapshot.filter_rejected_count,
        parser_fallback_count=snapshot.parser_fallback_count,
        title_timestamp_count=len(snapshot.episodes) - first_seen,
        first_seen_timestamp_count=first_seen,
        duration_known_count=len(known),
        duration_missing_count=len(snapshot.episodes) - len(known),
        observed_duration_missing_count=len(snapshot.evidence) - len(observed_known),
        min_duration_seconds=min(known) if known else None,
        max_duration_seconds=max(known) if known else None,
        duration_buckets=duration_profile(known),
        observed_duration_buckets=duration_profile(observed_known),
        warnings=tuple(warnings),
        episodes=evidence,
        public_status=public_status,
        public_observation=public_observation,
        public_release_mode=snapshot.public_release_mode,
        temporal_strategy=snapshot.temporal_strategy,
        temporal_supporting_rows=snapshot.temporal_supporting_rows,
        temporal_conflicts=snapshot.temporal_conflicts,
    )


def inspect_configured_source(
    source_id: SourceId,
    *,
    catalog: BuiltInCatalog,
    state: ManagedState,
    auth: _Auth,
    rate_limit: SpotifyRateLimitJournal,
    now: datetime,
    client_factory: Callable[[str], Any] | None = None,
    public_client: _PublicClient | None = None,
    observe_public: bool = True,
    metrics: SpotifyRequestMetrics | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> SourceInspection:
    """Manually audit any effective source, including a disabled custom source."""
    try:
        sources = effective_sources(catalog, state)
    except ManagedStateError:
        raise SourceInspectionError(InspectionFailure.CONFIGURATION) from None
    source = next((item for item in sources if item.id == source_id), None)
    if source is None:
        raise SourceInspectionError(InspectionFailure.CONFIGURATION)
    ownership = "built_in" if source_id in {item.id for item in catalog.sources} else "custom"
    return inspect_source(
        source,
        ownership=ownership,
        timezone=str(source.timezone),
        auth=auth,
        rate_limit=rate_limit,
        now=now,
        client_factory=client_factory,
        public_client=public_client,
        observe_public=observe_public,
        metrics=metrics,
        clock=clock,
    )


def serialize_source_inspection(report: SourceInspection) -> bytes:
    """Serialize only normalized, allow-listed V1 report fields as deterministic UTF-8 JSON."""

    def iso(value: datetime | None) -> str | None:
        return None if value is None else as_utc(value).isoformat()

    episodes = [
        {
            "catalogue_position": item.catalogue_position,
            "spotify_episode_id": item.spotify_episode_id,
            "spotify_episode_uri": item.spotify_episode_uri,
            "title": item.title,
            "title_truncated": item.title_truncated,
            "release_date": item.release_date,
            "release_date_precision": item.release_date_precision,
            "duration_seconds": item.duration_seconds,
            "collection_filter_outcome": item.collection_filter_outcome,
            "parser_outcome": item.parser_outcome,
            "public_release_outcome": item.public_release_outcome.value,
            "temporal_evidence": item.temporal_evidence,
            "temporal_supporting_rows": item.temporal_supporting_rows,
            "edition_at": iso(item.edition_at),
            "timestamp_source": (
                None if item.timestamp_source is None else item.timestamp_source.value
            ),
            "ordering_evidence": item.ordering_evidence,
            "warnings": list(item.warnings),
        }
        for item in report.episodes
    ]
    public = report.public_observation
    payload = {
        "schema_version": report.schema_version,
        "inspected_at": iso(report.inspected_at),
        "application_version": report.application_version,
        "build_revision": report.build_revision,
        "ownership": report.ownership,
        "source_id": report.source_id,
        "spotify_show_id": report.spotify_show_id,
        "spotify_uri": report.spotify_uri,
        "spotify_url": report.spotify_url,
        "display_name_hint": report.display_name_hint,
        "language_hints": list(report.language_hints),
        "publisher_hint": report.publisher_hint,
        "total_episodes_hint": report.total_episodes_hint,
        "parser_id": report.parser_id,
        "public_release_mode": report.public_release_mode.value,
        "temporal_strategy": report.temporal_strategy,
        "temporal_supporting_rows": report.temporal_supporting_rows,
        "temporal_conflicts": report.temporal_conflicts,
        "temporal_structure_detected": bool(report.temporal_supporting_rows),
        "temporal_coverage": report.temporal_supporting_rows / report.observed_episodes
        if report.observed_episodes
        else 0,
        "collection_filter_id": report.collection_filter_id,
        "timezone": report.timezone,
        "retention_hours": report.retention_hours,
        "cutoff_at": iso(report.cutoff_at),
        "page_size": report.page_size,
        "max_pages": report.max_pages,
        "catalogue_requests": report.catalogue_requests,
        "catalogue_complete": report.catalogue_complete,
        "coverage_proven": report.coverage_proven,
        "observed_episodes": report.observed_episodes,
        "accepted_episodes": report.accepted_episodes,
        "filter_rejected_count": report.filter_rejected_count,
        "parser_fallback_count": report.parser_fallback_count,
        "title_timestamp_count": report.title_timestamp_count,
        "first_seen_timestamp_count": report.first_seen_timestamp_count,
        "duration_known_count": report.duration_known_count,
        "duration_missing_count": report.duration_missing_count,
        "observed_duration_missing_count": report.observed_duration_missing_count,
        "min_duration_seconds": report.min_duration_seconds,
        "max_duration_seconds": report.max_duration_seconds,
        "duration_buckets": profile_as_dict(report.duration_buckets),
        "observed_duration_buckets": profile_as_dict(report.observed_duration_buckets),
        "warnings": list(report.warnings),
        "episodes": episodes,
        "public_status": report.public_status,
        "public_observation": None
        if public is None
        else {
            "episode_id": public.episode_id,
            "episode_uri": public.episode_uri,
            "title": public.title,
            "title_truncated": public.title_truncated,
            "duration_seconds": public.duration_seconds,
            "release_date": public.release_date,
            "release_date_truncated": public.release_date_truncated,
            "release_date_shape": public.release_date_shape,
            "detector_schema_version": public.detector_schema_version,
            "observed_at": iso(public.observed_at),
        },
    }
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
