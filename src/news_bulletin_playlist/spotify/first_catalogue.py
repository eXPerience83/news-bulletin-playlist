"""Official Spotify Web API catalogue ingestion for the Phase C shadow."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from news_bulletin_playlist.models import SourceDefinition
from news_bulletin_playlist.registry import get_collection_filter, get_title_parser
from news_bulletin_playlist.spotify.first_state import (
    SpotifyFirstEpisode,
    TimestampSource,
    as_utc,
    episode_order_key,
    release_period,
    spotify_id,
)
from news_bulletin_playlist.spotify.matcher import SpotifyCatalogClient, fetch_show_catalogue
from news_bulletin_playlist.spotify.public_detector import spotify_show_id

DEFAULT_MAX_PAGES = 4


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


def fetch_source_catalogue(
    client: SpotifyCatalogClient,
    source: SourceDefinition,
    *,
    now: datetime,
    cutoff: datetime,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> CatalogueSnapshot:
    """Reuse matcher paging validation while keeping identity/normalization Spotify-first."""
    observed = as_utc(now)
    cutoff = as_utc(cutoff)
    show_id = spotify_show_id(source)
    catalogue = fetch_show_catalogue(client, show_id, page_size=50, max_pages=max_pages)
    episodes: list[SpotifyFirstEpisode] = []
    ids: list[str] = []
    periods: list[tuple[date, date]] = []
    fallbacks = 0
    rejected = 0
    collection_filter = (
        None
        if source.collection_filter_id is None
        else get_collection_filter(source.collection_filter_id)
    )
    parser = get_title_parser(str(source.parser_id))

    for candidate in catalogue.candidates:
        episode_id = candidate.uri.removeprefix("spotify:episode:")
        spotify_id(episode_id)
        ids.append(episode_id)
        periods.append(release_period(candidate.release_date, candidate.release_date_precision))
        if collection_filter is not None and not collection_filter.accepts(candidate.name):
            rejected += 1
            continue
        parsed = parser.parse(candidate.name)
        timestamp_source = TimestampSource.FIRST_SEEN if parsed is None else TimestampSource.TITLE
        episodes.append(
            SpotifyFirstEpisode(
                source_id=source.id,
                spotify_show_id=show_id,
                spotify_episode_id=episode_id,
                spotify_episode_uri=candidate.uri,
                title=candidate.name,
                edition_at=None if parsed is None else parsed.edition_at,
                timestamp_source=timestamp_source,
                release_date_raw=candidate.release_date,
                release_date_precision=candidate.release_date_precision,
                duration_seconds=candidate.duration_seconds,
                first_seen_at=observed,
                last_seen_at=observed,
            )
        )
        fallbacks += int(parsed is None)

    latest = max(episodes, key=_api_recency_key, default=None)
    return CatalogueSnapshot(
        episodes=tuple(episodes),
        request_count=catalogue.request_count,
        complete=catalogue.complete,
        coverage_proven=catalogue.complete or _boundary_covers(periods, cutoff),
        api_ids=tuple(ids),
        api_latest_id=None if latest is None else latest.spotify_episode_id,
        parser_fallback_count=fallbacks,
        filter_rejected_count=rejected,
    )


def _api_recency_key(episode: SpotifyFirstEpisode) -> tuple[int, int, int]:
    return episode_order_key(episode)[:3]


def _boundary_covers(periods: list[tuple[date, date]], cutoff: datetime) -> bool:
    """Accept a bounded prefix only when observed release dates stay monotonic past retention."""
    if not periods:
        return False
    previous: date | None = None
    for _, end in periods:
        if previous is not None and end > previous:
            return False
        previous = end
    return periods[-1][1] < cutoff.date()
