"""Official Spotify Web API show-catalogue ingestion for the Spotify-first runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from news_bulletin_playlist.models import SourceDefinition
from news_bulletin_playlist.registry import get_collection_filter, get_title_parser
from news_bulletin_playlist.spotify.first_models import (
    SpotifyFirstEpisode,
    TimestampSource,
    as_utc,
    release_period,
    spotify_id,
)
from news_bulletin_playlist.spotify.public_detector import spotify_show_id
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


class CatalogueResponseError(RuntimeError):
    """Raised when Spotify returns a structurally invalid show catalogue."""


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


@dataclass(frozen=True, slots=True)
class _EpisodeCandidate:
    episode_id: str
    uri: str
    title: str
    release_date: str
    release_date_precision: str
    duration_seconds: int | None


def fetch_source_catalogue(
    client: SpotifyCatalogueClient,
    source: SourceDefinition,
    *,
    now: datetime,
    cutoff: datetime,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> CatalogueSnapshot:
    """Read a bounded official catalogue and normalize it directly to Spotify identity."""
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    observed = as_utc(now)
    retention_cutoff = as_utc(cutoff)
    show_id = spotify_show_id(source)
    candidates, request_count, complete = _fetch_catalogue(
        client,
        show_id,
        max_pages=max_pages,
    )

    collection_filter = (
        None
        if source.collection_filter_id is None
        else get_collection_filter(source.collection_filter_id)
    )
    parser = get_title_parser(str(source.parser_id))
    episodes: list[SpotifyFirstEpisode] = []
    periods: list[tuple[date, date]] = []
    api_ids: list[str] = []
    parser_fallbacks = 0
    filter_rejected = 0

    for candidate in candidates:
        api_ids.append(candidate.episode_id)
        periods.append(release_period(candidate.release_date, candidate.release_date_precision))
        if collection_filter is not None and not collection_filter.accepts(candidate.title):
            filter_rejected += 1
            continue

        parsed = parser.parse(candidate.title)
        timestamp_source = TimestampSource.TITLE
        edition_at = None if parsed is None else parsed.edition_at
        if parsed is None:
            timestamp_source = TimestampSource.FIRST_SEEN
            parser_fallbacks += 1

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

    return CatalogueSnapshot(
        episodes=tuple(episodes),
        request_count=request_count,
        complete=complete,
        coverage_proven=complete or _boundary_covers(periods, retention_cutoff),
        api_ids=tuple(api_ids),
        api_latest_id=_provable_latest_id(episodes),
        parser_fallback_count=parser_fallbacks,
        filter_rejected_count=filter_rejected,
    )


def _fetch_catalogue(
    client: SpotifyCatalogueClient,
    show_id: str,
    *,
    max_pages: int,
) -> tuple[tuple[_EpisodeCandidate, ...], int, bool]:
    candidates: list[_EpisodeCandidate] = []
    seen_ids: set[str] = set()
    expected_total: int | None = None

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

        for raw in items:
            candidate = _candidate_from_item(raw)
            if candidate.episode_id in seen_ids:
                raise CatalogueResponseError(
                    "Spotify catalogue repeated an episode id across pages"
                )
            seen_ids.add(candidate.episode_id)
            candidates.append(candidate)

        if next_page is None:
            return tuple(candidates), page_number + 1, True

    return tuple(candidates), max_pages, False


def _validate_page(
    page: object,
    *,
    requested_offset: int,
    expected_total: int | None,
) -> tuple[list[object], int, str | None]:
    if not isinstance(page, dict):
        raise CatalogueResponseError("Spotify show episodes response was not an object")
    items = page.get("items")
    if not isinstance(items, list):
        raise CatalogueResponseError("Spotify show episodes response lacked an item list")

    limit = page.get("limit")
    offset = page.get("offset")
    total = page.get("total")
    next_page = page.get("next")
    if isinstance(limit, bool) or limit != DEFAULT_PAGE_SIZE:
        raise CatalogueResponseError("Spotify show episodes response contained invalid limit")
    if isinstance(offset, bool) or offset != requested_offset:
        raise CatalogueResponseError("Spotify show episodes response contained invalid offset")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise CatalogueResponseError("Spotify show episodes response contained invalid total")
    if expected_total is not None and total != expected_total:
        raise CatalogueResponseError("Spotify show episodes total changed during pagination")
    if next_page is not None and (not isinstance(next_page, str) or not next_page.strip()):
        raise CatalogueResponseError("Spotify show episodes response contained invalid next")
    if len(items) > DEFAULT_PAGE_SIZE or requested_offset + len(items) > total:
        raise CatalogueResponseError("Spotify show episodes response contained impossible paging")

    has_more = requested_offset + len(items) < total
    if (next_page is not None) != has_more:
        raise CatalogueResponseError("Spotify show episodes paging contradicted next")
    return items, total, next_page


def _candidate_from_item(item: object) -> _EpisodeCandidate:
    if not isinstance(item, dict):
        raise CatalogueResponseError("Spotify episode item was not an object")

    episode_id = item.get("id")
    uri = item.get("uri")
    title = item.get("name")
    release_date = item.get("release_date")
    precision = item.get("release_date_precision")
    duration_ms = item.get("duration_ms")

    if not isinstance(episode_id, str):
        raise CatalogueResponseError("Spotify episode item contained invalid id")
    try:
        spotify_id(episode_id)
    except ValueError as exc:
        raise CatalogueResponseError("Spotify episode item contained invalid id") from exc

    if not isinstance(uri, str) or not is_spotify_episode_uri(uri):
        raise CatalogueResponseError("Spotify episode item contained invalid URI")
    if uri != f"spotify:episode:{episode_id}":
        raise CatalogueResponseError("Spotify episode item URI/id disagreed")
    if not isinstance(title, str) or not title.strip():
        raise CatalogueResponseError("Spotify episode item contained invalid title")
    if not isinstance(release_date, str) or not isinstance(precision, str):
        raise CatalogueResponseError("Spotify episode item contained invalid release date")
    try:
        release_period(release_date, precision)
    except ValueError as exc:
        raise CatalogueResponseError("Spotify episode release date was invalid") from exc

    duration_seconds: int | None = None
    if duration_ms is not None:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise CatalogueResponseError("Spotify episode item contained invalid duration")
        duration_seconds = round(duration_ms / 1000)

    return _EpisodeCandidate(
        episode_id=episode_id,
        uri=uri,
        title=title.strip(),
        release_date=release_date,
        release_date_precision=precision,
        duration_seconds=duration_seconds,
    )


def _provable_latest_id(episodes: list[SpotifyFirstEpisode]) -> str | None:
    """Return an ID only when available timestamp evidence proves one episode is newest."""
    if not episodes:
        return None
    for candidate in episodes:
        candidate_start, _ = _evidence_period(candidate)
        if all(
            other is candidate or candidate_start > _evidence_period(other)[1] for other in episodes
        ):
            return candidate.spotify_episode_id
    return None


def _evidence_period(episode: SpotifyFirstEpisode) -> tuple[datetime, datetime]:
    if episode.edition_at is not None:
        return episode.edition_at, episode.edition_at
    start, end = release_period(episode.release_date_raw, episode.release_date_precision)
    timezone = episode.first_seen_at.tzinfo
    return (
        datetime.combine(start, datetime.min.time(), tzinfo=timezone),
        datetime.combine(end, datetime.max.time(), tzinfo=timezone),
    )


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
