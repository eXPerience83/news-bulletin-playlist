"""Official Spotify Web API show-catalogue ingestion for the Spotify-first runtime."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import ParserId, PublicReleaseMode, TemporalStrategy
from news_bulletin_playlist.providers.base import CatalogueParseContext
from news_bulletin_playlist.providers.temporal_detector import TemporalSample
from news_bulletin_playlist.providers.title_recovery import TemporalEvidenceKind
from news_bulletin_playlist.registry import get_collection_filter, get_temporal_detector
from news_bulletin_playlist.spotify.catalogue_errors import (
    CatalogueItemValueKind,
    CatalogueValidationReason,
)
from news_bulletin_playlist.spotify.first_models import (
    SpotifyFirstEpisode,
    TimestampSource,
    as_utc,
    episode_evidence_period,
    release_period,
    spotify_id,
)
from news_bulletin_playlist.spotify.identifiers import SpotifyShowSource
from news_bulletin_playlist.spotify.public_detector import spotify_show_id
from news_bulletin_playlist.spotify.public_release import (
    PublicReleaseEvidence,
    PublicReleaseOutcome,
    validate_public_release,
)
from news_bulletin_playlist.spotify.uris import is_spotify_episode_uri

DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 4


class SpotifyCatalogueClient(Protocol):
    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]: ...


class CatalogueSource(SpotifyShowSource, Protocol):
    @property
    def public_release_mode(self) -> PublicReleaseMode: ...

    @property
    def parser_id(self) -> ParserId: ...

    @property
    def collection_filter_id(self) -> str | None: ...


class CatalogueResponseError(RuntimeError):
    """Safe validation cause; item_index is relative to its page and zero-based.

    Context contains only requested positions and already validated integer totals.
    No rejected value or free-form exception message is retained.
    """

    def __init__(
        self,
        reason: CatalogueValidationReason,
        *,
        requested_offset: int,
        item_index: int | None = None,
        expected_total: int | None = None,
        observed_total: int | None = None,
        item_value_kind: CatalogueItemValueKind | None = None,
    ) -> None:
        if not isinstance(reason, CatalogueValidationReason):
            raise ValueError("catalogue reason must be a closed validation code")
        for value in (requested_offset, item_index, expected_total, observed_total):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("catalogue context must contain non-negative integers")
        if reason is CatalogueValidationReason.ITEM_NOT_OBJECT:
            if item_index is None or not isinstance(item_value_kind, CatalogueItemValueKind):
                raise ValueError(
                    "non-object catalogue items require an index and closed value kind"
                )
        elif item_value_kind is not None:
            raise ValueError("catalogue item value kind is only valid for non-object items")
        self.item_value_kind = item_value_kind
        self.reason = reason
        self.requested_offset = requested_offset
        self.item_index = item_index
        self.expected_total = expected_total
        self.observed_total = observed_total
        super().__init__(f"Spotify catalogue validation failed: {reason.value}")


@dataclass(frozen=True, slots=True)
class CatalogueSnapshot:
    episodes: tuple[SpotifyFirstEpisode, ...]
    request_count: int
    complete: bool
    coverage_proven: bool
    api_ids: tuple[str, ...]
    api_latest_id: str | None
    parser_fallback_count: int
    filter_rejected_count: int
    evidence: tuple[CatalogueEpisodeEvidence, ...] = ()
    public_release_mode: PublicReleaseMode = PublicReleaseMode.INSTANT
    temporal_strategy: TemporalStrategy = TemporalStrategy.AUTO
    temporal_supporting_rows: int = 0

    temporal_conflicts: int = 0
    public_release_counts: tuple[tuple[PublicReleaseOutcome, int], ...] = ()
    null_item_count: int = 0

    def __post_init__(self) -> None:
        # Count only slots in the requested prefix, never hypothetical unseen content.
        if type(self.null_item_count) is not int or not (
            0 <= self.null_item_count <= self.request_count * DEFAULT_PAGE_SIZE - len(self.api_ids)
        ):
            raise ValueError("catalogue null count exceeds the observed prefix bound")
        if self.null_item_count and (
            self.complete or self.coverage_proven or self.api_latest_id is not None
        ):
            raise ValueError("unknown catalogue slots cannot carry source authority")


@dataclass(frozen=True, slots=True)
class CatalogueEpisodeEvidence:
    position: int  # One-based provider slot: requested offset + zero-based item index + 1.
    spotify_episode_id: str
    spotify_episode_uri: str
    title: str
    release_date: str
    release_date_precision: str
    duration_seconds: int | None
    filter_outcome: str
    parser_outcome: str
    edition_at: datetime | None
    timestamp_source: TimestampSource | None
    temporal_evidence: TemporalEvidenceKind | None = None
    temporal_supporting_rows: int = 0
    public_release_outcome: PublicReleaseOutcome = PublicReleaseOutcome.NO_EVIDENCE


@dataclass(frozen=True, slots=True)
class _EpisodeCandidate:
    position: int
    episode_id: str
    uri: str
    title: str
    release_date: str
    release_date_precision: str
    duration_seconds: int | None


def fetch_source_catalogue(
    client: SpotifyCatalogueClient,
    source: CatalogueSource,
    *,
    now: datetime,
    cutoff: datetime,
    max_pages: int = DEFAULT_MAX_PAGES,
    collect_evidence: bool = False,
    public_release_evidence: PublicReleaseEvidence | None = None,
) -> CatalogueSnapshot:
    """Read a bounded official catalogue and normalize it directly to Spotify identity."""
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    observed = as_utc(now)
    retention_cutoff = as_utc(cutoff)
    show_id = spotify_show_id(source)
    candidates, request_count, complete, null_item_count = _fetch_catalogue(
        client,
        show_id,
        max_pages=max_pages,
    )

    collection_filter = (
        None
        if source.collection_filter_id is None
        else get_collection_filter(source.collection_filter_id)
    )
    episodes: list[SpotifyFirstEpisode] = []
    periods: list[tuple[date, date]] = []
    api_ids: list[str] = []
    parser_fallbacks = 0
    filter_rejected = 0
    evidence: list[CatalogueEpisodeEvidence] = []
    public_release_counts: Counter[PublicReleaseOutcome] = Counter()

    source_timezone = getattr(source, "timezone", None)
    catalogue_timezone = source_timezone if isinstance(source_timezone, ZoneInfo) else None

    detector = get_temporal_detector(
        tuple(
            TemporalSample(
                candidate.title,
                CatalogueParseContext(
                    candidate.release_date, candidate.release_date_precision, catalogue_timezone
                ),
            )
            for candidate in candidates
        ),
        strategy=getattr(source, "temporal_strategy", TemporalStrategy.AUTO),
    )

    for candidate in candidates:
        position = candidate.position
        api_ids.append(candidate.episode_id)
        periods.append(release_period(candidate.release_date, candidate.release_date_precision))
        if collection_filter is not None and not collection_filter.accepts(candidate.title):
            filter_rejected += 1
            if collect_evidence:
                evidence.append(
                    CatalogueEpisodeEvidence(
                        position,
                        candidate.episode_id,
                        candidate.uri,
                        candidate.title,
                        candidate.release_date,
                        candidate.release_date_precision,
                        candidate.duration_seconds,
                        "rejected",
                        "not_evaluated",
                        None,
                        None,
                    )
                )
            continue

        attempt = detector.parse(
            candidate.title,
            context=CatalogueParseContext(
                release_date=candidate.release_date,
                release_date_precision=candidate.release_date_precision,
                timezone=catalogue_timezone,
            ),
        )
        parsed = attempt.parsed
        timestamp_source = TimestampSource.TITLE
        edition_at = None if parsed is None else parsed.edition_at
        parser_outcome: str = attempt.mode
        if parsed is None:
            timestamp_source = TimestampSource.FIRST_SEEN
            parser_fallbacks += 1
            parser_outcome = "fallback"

        public_at, public_outcome = validate_public_release(
            public_release_evidence,
            source_id=source.id,
            show_id=show_id,
            episode_id=candidate.episode_id,
            release_date=candidate.release_date,
            precision=candidate.release_date_precision,
            timezone=catalogue_timezone,
            semantic_exact=parsed is not None,
            mode=source.public_release_mode,
        )
        if public_at is not None:
            edition_at = public_at
            timestamp_source = TimestampSource.SPOTIFY_PUBLIC_RELEASE
        public_release_counts[public_outcome] += 1

        episodes.append(
            SpotifyFirstEpisode(
                source_id=source.id,
                spotify_show_id=show_id,
                spotify_episode_id=candidate.episode_id,
                spotify_episode_uri=candidate.uri,
                title=candidate.title,
                edition_at=edition_at,
                timestamp_source=timestamp_source,
                release_date_raw=candidate.release_date,
                release_date_precision=candidate.release_date_precision,
                duration_seconds=candidate.duration_seconds,
                first_seen_at=observed,
                last_seen_at=observed,
            )
        )
        if collect_evidence:
            evidence.append(
                CatalogueEpisodeEvidence(
                    position,
                    candidate.episode_id,
                    candidate.uri,
                    candidate.title,
                    candidate.release_date,
                    candidate.release_date_precision,
                    candidate.duration_seconds,
                    "accepted",
                    parser_outcome,
                    episodes[-1].edition_at,
                    episodes[-1].timestamp_source,
                    attempt.temporal_evidence,
                    attempt.supporting_rows,
                    public_outcome,
                )
            )

    return CatalogueSnapshot(
        episodes=tuple(episodes),
        request_count=request_count,
        complete=complete and not null_item_count,
        coverage_proven=not null_item_count
        and (complete or _boundary_covers(periods, retention_cutoff)),
        api_ids=tuple(api_ids),
        api_latest_id=None
        if null_item_count
        else _provable_latest_id(episodes, catalogue_timezone),
        null_item_count=null_item_count,
        parser_fallback_count=parser_fallbacks,
        filter_rejected_count=filter_rejected,
        evidence=tuple(evidence),
        public_release_mode=source.public_release_mode,
        temporal_strategy=detector.strategy,
        temporal_supporting_rows=detector.supporting_rows,
        temporal_conflicts=detector.conflicts,
        public_release_counts=tuple(sorted(public_release_counts.items())),
    )


def apply_public_release_evidence(
    snapshot: CatalogueSnapshot, source: CatalogueSource, evidence: PublicReleaseEvidence
) -> CatalogueSnapshot:
    """Enrich an already fetched audit snapshot without repeating any request."""
    timezone = getattr(source, "timezone", None)
    zone = timezone if isinstance(timezone, ZoneInfo) else None
    episodes = []
    outcomes = {}
    for episode in snapshot.episodes:
        instant, outcome = validate_public_release(
            evidence,
            source_id=episode.source_id,
            show_id=episode.spotify_show_id,
            episode_id=episode.spotify_episode_id,
            release_date=episode.release_date_raw,
            precision=episode.release_date_precision,
            timezone=zone,
            semantic_exact=episode.timestamp_source is TimestampSource.TITLE,
            mode=source.public_release_mode,
        )
        outcomes[episode.spotify_episode_id] = outcome
        episodes.append(
            episode
            if instant is None
            else replace(
                episode, edition_at=instant, timestamp_source=TimestampSource.SPOTIFY_PUBLIC_RELEASE
            )
        )
    by_id = {episode.spotify_episode_id: episode for episode in episodes}
    rows = tuple(
        row
        if row.spotify_episode_id not in by_id
        else replace(
            row,
            edition_at=by_id[row.spotify_episode_id].edition_at,
            timestamp_source=by_id[row.spotify_episode_id].timestamp_source,
            public_release_outcome=outcomes[row.spotify_episode_id],
        )
        for row in snapshot.evidence
    )
    return replace(
        snapshot,
        episodes=tuple(episodes),
        evidence=rows,
        api_latest_id=None if snapshot.null_item_count else _provable_latest_id(episodes, zone),
        public_release_counts=tuple(sorted(Counter(outcomes.values()).items())),
    )


def _fetch_catalogue(
    client: SpotifyCatalogueClient,
    show_id: str,
    *,
    max_pages: int,
) -> tuple[tuple[_EpisodeCandidate, ...], int, bool, int]:
    candidates: list[_EpisodeCandidate] = []
    seen_ids: set[str] = set()
    expected_total: int | None = None
    null_item_count = 0

    for page_number in range(max_pages):
        offset = page_number * DEFAULT_PAGE_SIZE
        page = client.show_episodes(
            show_id,
            limit=DEFAULT_PAGE_SIZE,
            offset=offset,
        )
        items, total, next_page = _validate_page(
            page,
            requested_offset=offset,
            expected_total=expected_total,
        )
        if expected_total is None:
            expected_total = total

        for item_index, raw in enumerate(items):
            if raw is None:
                null_item_count += 1
                continue
            candidate = _candidate_from_item(raw, requested_offset=offset, item_index=item_index)
            if candidate.episode_id in seen_ids:
                raise CatalogueResponseError(
                    CatalogueValidationReason.DUPLICATE_EPISODE_ID,
                    requested_offset=offset,
                    item_index=item_index,
                )
            seen_ids.add(candidate.episode_id)
            candidates.append(candidate)

        if next_page is None:
            return tuple(candidates), page_number + 1, True, null_item_count

    return tuple(candidates), max_pages, False, null_item_count


def _validate_page(
    page: object,
    *,
    requested_offset: int,
    expected_total: int | None,
) -> tuple[list[object], int, str | None]:
    if not isinstance(page, dict):
        raise CatalogueResponseError(
            CatalogueValidationReason.PAGE_NOT_OBJECT, requested_offset=requested_offset
        )
    items = page.get("items")
    if not isinstance(items, list):
        raise CatalogueResponseError(
            CatalogueValidationReason.ITEMS_NOT_LIST, requested_offset=requested_offset
        )

    limit = page.get("limit")
    offset = page.get("offset")
    total = page.get("total")
    next_page = page.get("next")
    if isinstance(limit, bool) or limit != DEFAULT_PAGE_SIZE:
        raise CatalogueResponseError(
            CatalogueValidationReason.INVALID_LIMIT, requested_offset=requested_offset
        )
    if isinstance(offset, bool) or offset != requested_offset:
        raise CatalogueResponseError(
            CatalogueValidationReason.INVALID_OFFSET, requested_offset=requested_offset
        )
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise CatalogueResponseError(
            CatalogueValidationReason.INVALID_TOTAL, requested_offset=requested_offset
        )
    if expected_total is not None and total != expected_total:
        raise CatalogueResponseError(
            CatalogueValidationReason.TOTAL_CHANGED,
            requested_offset=requested_offset,
            expected_total=expected_total,
            observed_total=total,
        )
    if next_page is not None and (not isinstance(next_page, str) or not next_page.strip()):
        raise CatalogueResponseError(
            CatalogueValidationReason.INVALID_NEXT, requested_offset=requested_offset
        )
    if len(items) > DEFAULT_PAGE_SIZE or requested_offset + len(items) > total:
        raise CatalogueResponseError(
            CatalogueValidationReason.IMPOSSIBLE_PAGING, requested_offset=requested_offset
        )

    has_more = requested_offset + len(items) < total
    if (next_page is not None) != has_more:
        raise CatalogueResponseError(
            CatalogueValidationReason.NEXT_CONTRADICTION, requested_offset=requested_offset
        )
    return items, total, next_page


def _non_object_item_value_kind(item: object) -> CatalogueItemValueKind:
    """Classify locally without reading content, repr or dynamic type names."""
    if item is None:
        return CatalogueItemValueKind.NULL
    if isinstance(item, bool):  # bool is an int subclass: check it before numbers.
        return CatalogueItemValueKind.BOOLEAN
    if isinstance(item, list):
        return CatalogueItemValueKind.ARRAY
    if isinstance(item, str):
        return CatalogueItemValueKind.STRING
    if isinstance(item, int | float):
        return CatalogueItemValueKind.NUMBER
    return CatalogueItemValueKind.OTHER


def _candidate_from_item(
    item: object, *, requested_offset: int, item_index: int
) -> _EpisodeCandidate:
    if not isinstance(item, dict):
        raise CatalogueResponseError(
            CatalogueValidationReason.ITEM_NOT_OBJECT,
            requested_offset=requested_offset,
            item_index=item_index,
            item_value_kind=_non_object_item_value_kind(item),
        )

    episode_id = item.get("id")
    uri = item.get("uri")
    title = item.get("name")
    release_date = item.get("release_date")
    precision = item.get("release_date_precision")
    duration_ms = item.get("duration_ms")

    if not isinstance(episode_id, str):
        raise CatalogueResponseError(
            CatalogueValidationReason.INVALID_EPISODE_ID,
            requested_offset=requested_offset,
            item_index=item_index,
        )
    try:
        spotify_id(episode_id)
    except ValueError as exc:
        raise CatalogueResponseError(
            CatalogueValidationReason.INVALID_EPISODE_ID,
            requested_offset=requested_offset,
            item_index=item_index,
        ) from exc

    if not isinstance(uri, str) or not is_spotify_episode_uri(uri):
        raise CatalogueResponseError(
            CatalogueValidationReason.INVALID_EPISODE_URI,
            requested_offset=requested_offset,
            item_index=item_index,
        )
    if uri != f"spotify:episode:{episode_id}":
        raise CatalogueResponseError(
            CatalogueValidationReason.EPISODE_URI_ID_MISMATCH,
            requested_offset=requested_offset,
            item_index=item_index,
        )
    if not isinstance(title, str) or not title.strip():
        raise CatalogueResponseError(
            CatalogueValidationReason.INVALID_TITLE,
            requested_offset=requested_offset,
            item_index=item_index,
        )
    if not isinstance(release_date, str):
        raise CatalogueResponseError(
            CatalogueValidationReason.INVALID_RELEASE_DATE,
            requested_offset=requested_offset,
            item_index=item_index,
        )
    if not isinstance(precision, str) or precision not in {"day", "month", "year"}:
        raise CatalogueResponseError(
            CatalogueValidationReason.INVALID_RELEASE_PRECISION,
            requested_offset=requested_offset,
            item_index=item_index,
        )
    try:
        release_period(release_date, precision)
    except ValueError as exc:
        raise CatalogueResponseError(
            CatalogueValidationReason.INVALID_RELEASE_DATE,
            requested_offset=requested_offset,
            item_index=item_index,
        ) from exc

    duration_seconds: int | None = None
    if duration_ms is not None:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise CatalogueResponseError(
                CatalogueValidationReason.INVALID_DURATION,
                requested_offset=requested_offset,
                item_index=item_index,
            )
        duration_seconds = round(duration_ms / 1000)

    return _EpisodeCandidate(
        position=requested_offset + item_index + 1,
        episode_id=episode_id,
        uri=uri,
        title=title.strip(),
        release_date=release_date,
        release_date_precision=precision,
        duration_seconds=duration_seconds,
    )


def _provable_latest_id(
    episodes: list[SpotifyFirstEpisode], timezone: ZoneInfo | None
) -> str | None:
    """Return an ID only when available timestamp evidence proves one episode is newest."""
    if not episodes:
        return None
    for candidate in episodes:
        candidate_start, _ = episode_evidence_period(candidate, timezone)
        if all(
            other is candidate or candidate_start > episode_evidence_period(other, timezone)[1]
            for other in episodes
        ):
            return candidate.spotify_episode_id
    return None


def _boundary_covers(periods: list[tuple[date, date]], cutoff: datetime) -> bool:
    """Treat a bounded prefix as sufficient only after observed release dates stay monotonic."""
    if not periods:
        return False
    previous_end: date | None = None
    for _, end in periods:
        if previous_end is not None and end > previous_end:
            return False
        previous_end = end
    return periods[-1][1] < cutoff.date()
