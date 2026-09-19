"""Bounded operational diagnostics for the Spotify-first Phase C shadow."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from news_bulletin_playlist.diagnostics import DiagnosticSeverity, DiagnosticValue
from news_bulletin_playlist.models import PlaylistDefinition, SourceDefinition, SourceId
from news_bulletin_playlist.providers.release_date_title import RELEASE_DATE_TITLE_PARSER_ID
from news_bulletin_playlist.registry import get_title_parser
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError
from news_bulletin_playlist.spotify.first_catalogue import CatalogueSnapshot
from news_bulletin_playlist.spotify.first_desired import (
    DesiredComparison,
    ShadowDesiredState,
)
from news_bulletin_playlist.spotify.first_models import (
    RefreshReason,
    SpotifyFirstEpisode,
    TimestampSource,
    UpsertCounts,
    release_period,
)
from news_bulletin_playlist.spotify.matcher import MatchResponseError
from news_bulletin_playlist.spotify.public_detector import PublicDetectorState
from news_bulletin_playlist.persistence import PersistenceError


def emit_refresh(
    diagnostics: OperationalDiagnostics,
    source: SourceDefinition,
    reason: RefreshReason,
    public: PublicDetectorState,
    snapshot: CatalogueSnapshot,
    counts: UpsertCounts,
    *,
    now: datetime,
) -> None:
    details: dict[str, DiagnosticValue] = {
        "refresh_reason": reason.value,
        "catalogue_request_count": snapshot.request_count,
        "catalogue_complete": snapshot.complete,
        "catalogue_coverage_proven": snapshot.coverage_proven,
        "episode_count": len(snapshot.episodes),
        "episode_added_count": counts.added,
        "episode_updated_count": counts.updated,
        "episode_unchanged_count": counts.unchanged,
        "parser_fallback_count": snapshot.parser_fallback_count,
        "filter_rejected_count": snapshot.filter_rejected_count,
    }
    if public.last_episode_id is not None:
        details["spotify_episode_id"] = public.last_episode_id
    if snapshot.api_latest_id is not None:
        details["api_episode_id"] = snapshot.api_latest_id
    diagnostics.emit(
        occurred_at=now,
        severity=DiagnosticSeverity.INFO,
        component="spotify.first_shadow",
        event_name="spotify_first_shadow_catalogue_refreshed",
        source_id=str(source.id),
        details=details,
    )


def emit_parser_fallbacks(
    diagnostics: OperationalDiagnostics,
    source: SourceDefinition,
    episodes: Sequence[SpotifyFirstEpisode],
    *,
    now: datetime,
) -> None:
    if str(source.parser_id) == RELEASE_DATE_TITLE_PARSER_ID:
        return
    fallback = [
        episode
        for episode in episodes
        if episode.timestamp_source is TimestampSource.FIRST_SEEN
    ][:5]
    for episode in fallback:
        diagnostics.emit(
            occurred_at=now,
            severity=DiagnosticSeverity.INFO,
            component="spotify.first_shadow",
            event_name="spotify_first_shadow_parser_fallback",
            source_id=str(source.id),
            details={
                "spotify_episode_id": episode.spotify_episode_id,
                "spotify_episode_title": episode.title,
                "spotify_release_date": episode.release_date_raw,
                "spotify_release_date_precision": episode.release_date_precision,
                "timestamp_source": episode.timestamp_source.value,
            },
        )


def emit_detector_correlation(
    diagnostics: OperationalDiagnostics,
    source: SourceDefinition,
    reason: RefreshReason,
    public: PublicDetectorState,
    snapshot: CatalogueSnapshot,
    *,
    now: datetime,
) -> None:
    detector_id = public.last_episode_id
    if detector_id is None:
        return
    detector_present = detector_id in set(snapshot.api_ids)
    details: dict[str, DiagnosticValue] = {
        "refresh_reason": reason.value,
        "detector_api_match": detector_present,
        "spotify_episode_id": detector_id,
    }
    if snapshot.api_latest_id is not None:
        details["api_episode_id"] = snapshot.api_latest_id
    diagnostics.emit(
        occurred_at=now,
        severity=DiagnosticSeverity.INFO if detector_present else DiagnosticSeverity.WARNING,
        component="spotify.first_shadow",
        event_name="spotify_first_shadow_detector_api_correlation",
        source_id=str(source.id),
        details=details,
    )
    if reason is not RefreshReason.INACTIVITY_AUDIT or snapshot.api_latest_id is None:
        return

    false_negative_reason = _false_negative_reason(source, public, snapshot)
    if false_negative_reason is not None:
        diagnostics.emit(
            occurred_at=now,
            severity=DiagnosticSeverity.WARNING,
            component="spotify.first_shadow",
            event_name="spotify_first_shadow_possible_detector_false_negative",
            source_id=str(source.id),
            details={
                "refresh_reason": reason.value,
                "false_negative_reason": false_negative_reason,
                "spotify_episode_id": detector_id,
                "api_episode_id": snapshot.api_latest_id,
            },
        )


def emit_playlist_comparison(
    diagnostics: OperationalDiagnostics,
    playlist: PlaylistDefinition,
    comparison: DesiredComparison,
    legacy_items: Sequence[object],
    shadow: ShadowDesiredState,
    *,
    now: datetime,
) -> None:
    diagnostics.emit(
        occurred_at=now,
        severity=(
            DiagnosticSeverity.INFO
            if comparison.equivalent and comparison.valid
            else DiagnosticSeverity.WARNING
        ),
        component="spotify.first_shadow",
        event_name="spotify_first_shadow_playlist_comparison",
        playlist_id=str(playlist.id),
        details={
            "legacy_count": comparison.legacy_count,
            "shadow_count": comparison.shadow_count,
            "missing_count": comparison.missing_count,
            "extra_count": comparison.extra_count,
            "order_difference_count": comparison.order_difference_count,
            "duration_unavailable_count": comparison.duration_unavailable_count,
            "legacy_fingerprint": comparison.legacy_fingerprint,
            "shadow_fingerprint": comparison.shadow_fingerprint,
            "comparison_equivalent": comparison.equivalent,
            "comparison_valid": comparison.valid,
        },
    )
    del legacy_items, shadow
    for source_id in comparison.differing_source_ids:
        diagnostics.emit(
            occurred_at=now,
            severity=DiagnosticSeverity.WARNING,
            component="spotify.first_shadow",
            event_name="spotify_first_shadow_source_difference",
            source_id=str(source_id),
            playlist_id=str(playlist.id),
            details={"comparison_valid": comparison.valid},
        )


def emit_failure(
    diagnostics: OperationalDiagnostics,
    event_name: str,
    exc: Exception,
    *,
    now: datetime,
    phase: str,
    source_id: SourceId | None = None,
    playlist_id: str | None = None,
    refresh_reason: RefreshReason | None = None,
) -> None:
    details: dict[str, DiagnosticValue] = {
        "phase": phase,
        "failure_class": failure_class(exc),
    }
    if refresh_reason is not None:
        details["refresh_reason"] = refresh_reason.value
    diagnostics.emit(
        occurred_at=now,
        severity=DiagnosticSeverity.WARNING,
        component="spotify.first_shadow",
        event_name=event_name,
        source_id=None if source_id is None else str(source_id),
        playlist_id=playlist_id,
        details=details,
    )


def failure_class(exc: Exception) -> str:
    if isinstance(exc, SpotifyApiError):
        return "api_error"
    if isinstance(exc, SpotifyTransportError):
        return "transport_error"
    if isinstance(exc, MatchResponseError):
        return "response_shape_error"
    if isinstance(exc, PersistenceError):
        return "persistence_error"
    return "configuration_error"


def _false_negative_reason(
    source: SourceDefinition,
    public: PublicDetectorState,
    snapshot: CatalogueSnapshot,
) -> str | None:
    detector_id = public.last_episode_id
    api_id = snapshot.api_latest_id
    if detector_id is None or api_id is None or detector_id == api_id:
        return None
    api_episode = next(
        (episode for episode in snapshot.episodes if episode.spotify_episode_id == api_id),
        None,
    )
    if api_episode is None:
        return None

    detector_parsed = (
        None
        if public.last_episode_title is None
        else get_title_parser(str(source.parser_id)).parse(public.last_episode_title)
    )
    if (
        detector_parsed is not None
        and api_episode.edition_at is not None
        and api_episode.edition_at > detector_parsed.edition_at
    ):
        return "api_semantic_newer"

    detector_day = _date_prefix(public.last_public_release_date_raw)
    if detector_day is not None:
        _api_start, api_end = release_period(
            api_episode.release_date_raw,
            api_episode.release_date_precision,
        )
        if api_end > detector_day:
            return "api_release_day_newer"

    if detector_id not in set(snapshot.api_ids) and snapshot.coverage_proven:
        return "detector_id_missing"
    return None


def _date_prefix(value: str | None) -> date | None:
    if value is None or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
