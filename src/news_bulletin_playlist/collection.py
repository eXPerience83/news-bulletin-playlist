from __future__ import annotations

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import (
    CanonicalEdition,
    EngineConfig,
    ParsedEdition,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.registry import get_title_parser

FeedFetcher = Callable[[str], bytes]

_USER_AGENT = (
    "news-bulletin-playlist/0.0.1 "
    "(+https://github.com/eXPerience83/news-bulletin-playlist)"
)
_MAX_FEED_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SourceCollectionResult:
    """Outcome for one source during a shared collection cycle."""

    source_id: SourceId
    editions: tuple[CanonicalEdition, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class CollectionCycleResult:
    """Per-source results for one fetch-once collection cycle."""

    sources: tuple[SourceCollectionResult, ...]

    @property
    def editions(self) -> tuple[CanonicalEdition, ...]:
        return tuple(edition for result in self.sources for edition in result.editions)


def required_sources(config: EngineConfig) -> tuple[SourceDefinition, ...]:
    """Return each source required by enabled playlists exactly once, in config order."""

    required_ids = {
        source_id
        for playlist in config.playlists
        if playlist.enabled
        for source_id in playlist.source_selection.explicit
    }
    return tuple(
        source for source in config.sources if source.enabled and source.id in required_ids
    )


def fetch_feed(url: str, timeout: float = 20.0) -> bytes:
    """Fetch a bounded RSS payload without applying provider or playlist policy."""

    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read(_MAX_FEED_BYTES + 1)
    if len(payload) > _MAX_FEED_BYTES:
        raise ValueError("feed payload exceeds 10 MiB limit")
    return bytes(payload)


def collect_required_sources(
    config: EngineConfig,
    *,
    fetcher: FeedFetcher = fetch_feed,
) -> CollectionCycleResult:
    """Fetch and normalize the union of sources required by enabled playlists."""

    results: list[SourceCollectionResult] = []
    for source in required_sources(config):
        results.append(_collect_source(source, fetcher))
    return CollectionCycleResult(tuple(results))


def _collect_source(source: SourceDefinition, fetcher: FeedFetcher) -> SourceCollectionResult:
    if source.endpoint_url is None:
        return SourceCollectionResult(source.id, error="required source has no endpoint_url")

    try:
        payload = fetcher(source.endpoint_url)
        editions = normalize_rss_source(source, payload)
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        ET.ParseError,
        ValueError,
        KeyError,
    ) as exc:
        detail = str(exc).strip() or type(exc).__name__
        return SourceCollectionResult(source.id, error=detail)
    return SourceCollectionResult(source.id, editions=editions)


def normalize_rss_source(
    source: SourceDefinition,
    payload: bytes,
) -> tuple[CanonicalEdition, ...]:
    """Normalize one RSS payload into source-native canonical bulletin editions."""

    root = ET.fromstring(payload)
    items = [element for element in root.iter() if _local_name(element.tag) == "item"]
    if not items:
        raise ValueError("feed contained no RSS items")

    parser = get_title_parser(str(source.parser_id))
    editions: list[CanonicalEdition] = []
    seen_native_ids: set[str] = set()

    for item in items:
        title = _child_text(item, "title")
        source_native_id = _source_native_id(item)
        published_text = _first_child_text(item, ("pubdate", "published", "date"))
        if title is None or source_native_id is None or published_text is None:
            continue
        if source_native_id in seen_native_ids:
            continue

        parsed = parser.parse(title)
        if parsed is None:
            continue
        try:
            published_at = _parse_published_at(published_text, source.timezone)
        except ValueError:
            continue

        editions.append(
            CanonicalEdition(
                source_id=source.id,
                source_native_id=source_native_id,
                title=title,
                published_at=published_at,
                edition_at=_apply_source_timezone(parsed, source.timezone),
                duration_seconds=_duration_seconds(item),
            )
        )
        seen_native_ids.add(source_native_id)

    if not editions:
        raise ValueError("feed contained no canonical bulletin editions")
    return tuple(editions)


def _source_native_id(item: ET.Element) -> str | None:
    for name in ("guid", "id"):
        value = _child_text(item, name)
        if value is not None:
            return value

    enclosure = _child(item, "enclosure")
    if enclosure is not None:
        value = _nonempty(enclosure.attrib.get("url"))
        if value is not None:
            return value

    link = _child(item, "link")
    if link is not None:
        value = _element_text(link) or _nonempty(link.attrib.get("href"))
        if value is not None:
            return value
    return None


def _parse_published_at(value: str, source_timezone: ZoneInfo) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid publication timestamp: {value!r}") from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=source_timezone)
    return parsed


def _apply_source_timezone(parsed: ParsedEdition, source_timezone: ZoneInfo) -> datetime:
    local_wall_clock = parsed.edition_at.replace(tzinfo=None)
    return local_wall_clock.replace(tzinfo=source_timezone)


def _duration_seconds(item: ET.Element) -> int | None:
    value = _child_text(item, "duration")
    if value is None:
        return None

    parts = value.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if any(number < 0 for number in numbers):
        return None

    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        minutes, seconds = numbers
        return minutes * 60 + seconds if seconds < 60 else None
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        if minutes >= 60 or seconds >= 60:
            return None
        return hours * 3600 + minutes * 60 + seconds
    return None


def _first_child_text(item: ET.Element, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = _child_text(item, name)
        if value is not None:
            return value
    return None


def _child_text(item: ET.Element, name: str) -> str | None:
    child = _child(item, name)
    return None if child is None else _element_text(child)


def _child(item: ET.Element, name: str) -> ET.Element | None:
    wanted = name.lower()
    for child in item:
        if _local_name(child.tag) == wanted:
            return child
    return None


def _element_text(element: ET.Element) -> str | None:
    return _nonempty("".join(element.itertext()))


def _nonempty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()
