from __future__ import annotations

import gzip
import io
import socket
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import (
    CanonicalEdition,
    EngineConfig,
    ParsedEdition,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.providers.release_date_title import RELEASE_DATE_TITLE_PARSER_ID
from news_bulletin_playlist.registry import get_collection_filter, get_recovery_title_parser

FeedFetcher = Callable[[str], bytes]
type CollectionFailureReason = Literal[
    "missing_endpoint",
    "fetch_error",
    "xml_parse_error",
    "no_rss_items",
    "no_canonical_editions",
    "title_parser_drift",
    "configuration_error",
    "normalization_error",
]
type RssFetchFailureKind = Literal[
    "timeout",
    "dns",
    "tls",
    "connection",
    "http_4xx",
    "http_5xx",
    "redirect_error",
    "payload_too_large",
    "invalid_gzip",
    "other_io",
]

_USER_AGENT = (
    "news-bulletin-playlist/0.0.1 (+https://github.com/eXPerience83/news-bulletin-playlist)"
)
_MAX_FEED_BYTES = 10 * 1024 * 1024
_GZIP_MAGIC = b"\x1f\x8b"
_FETCH_DURATION_ATTR = "_news_playlist_rss_fetch_duration_ms"
_FETCH_REDIRECT_ATTR = "_news_playlist_rss_redirect_count"
_FETCH_FAILURE_KIND_ATTR = "_news_playlist_rss_fetch_failure_kind"
_REDIRECT_DISPATCH_ATTR = "_news_playlist_rss_redirect_dispatch"
_TITLE_PARSE_MIN_SAMPLE = 3
_TITLE_PARSE_MIN_ACCEPT_RATIO = 0.5
_RSS_FETCH_FAILURE_KINDS = frozenset(
    {
        "timeout",
        "dns",
        "tls",
        "connection",
        "http_4xx",
        "http_5xx",
        "redirect_error",
        "payload_too_large",
        "invalid_gzip",
        "other_io",
    }
)


@dataclass(frozen=True, slots=True)
class CollectionDiagnostics:
    """Bounded, provider-value-free observations from one collection attempt."""

    rss_item_count: int | None = None
    rss_missing_required_fields_count: int | None = None
    rss_filter_rejected_count: int | None = None
    rss_duplicate_native_id_count: int | None = None
    rss_invalid_published_at_count: int | None = None
    rss_title_parser_attempted_count: int | None = None
    rss_title_parser_exact_count: int | None = None
    rss_title_parser_recovered_count: int | None = None
    rss_title_parser_normalized_recovered_count: int | None = None
    rss_title_parser_provider_compat_recovered_count: int | None = None
    rss_title_parser_rejected_count: int | None = None
    rss_native_id_guid_count: int | None = None
    rss_native_id_enclosure_count: int | None = None
    rss_native_id_link_count: int | None = None
    rss_fetch_duration_ms: int | None = None
    rss_fetch_failure_kind: RssFetchFailureKind | None = None
    rss_redirect_count: int | None = None
    failure_reason: CollectionFailureReason | None = None


@dataclass(frozen=True, slots=True)
class SourceCollectionResult:
    """Outcome for one source during a shared collection cycle."""

    source_id: SourceId
    editions: tuple[CanonicalEdition, ...] = ()
    error: str | None = None
    collection_diagnostics: CollectionDiagnostics = field(default_factory=CollectionDiagnostics)

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class CollectionCycleResult:
    """Per-source results from one fetch-once collection cycle."""

    sources: tuple[SourceCollectionResult, ...]

    @property
    def editions(self) -> tuple[CanonicalEdition, ...]:
        return tuple(edition for result in self.sources for edition in result.editions)


@dataclass(frozen=True, slots=True)
class _NormalizedSource:
    editions: tuple[CanonicalEdition, ...]
    diagnostics: CollectionDiagnostics
    error: Exception | None = None


class _ObservedFeedPayload(bytes):
    rss_fetch_duration_ms: int
    rss_redirect_count: int

    def __new__(
        cls,
        payload: bytes,
        *,
        rss_fetch_duration_ms: int,
        rss_redirect_count: int,
    ) -> _ObservedFeedPayload:
        observed = super().__new__(cls, payload)
        observed.rss_fetch_duration_ms = rss_fetch_duration_ms
        observed.rss_redirect_count = rss_redirect_count
        return observed


class _RedirectCounter(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.redirect_count = 0

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            setattr(redirected, _REDIRECT_DISPATCH_ATTR, True)
        return redirected

    def _observe_redirect_dispatch(
        self,
        req: urllib.request.Request,
    ) -> urllib.request.Request:
        if getattr(req, _REDIRECT_DISPATCH_ATTR, False):
            self.redirect_count += 1
            delattr(req, _REDIRECT_DISPATCH_ATTR)
        return req

    def http_request(self, req: urllib.request.Request) -> urllib.request.Request:
        return self._observe_redirect_dispatch(req)

    def https_request(self, req: urllib.request.Request) -> urllib.request.Request:
        return self._observe_redirect_dispatch(req)

    def ftp_request(self, req: urllib.request.Request) -> urllib.request.Request:
        return self._observe_redirect_dispatch(req)


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
    """Fetch a bounded RSS payload and safely decode gzip when providers use it."""

    started_at = time.monotonic()
    redirect_counter = _RedirectCounter()
    opener = urllib.request.build_opener(redirect_counter)
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Encoding": "gzip",
            },
        )
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(_MAX_FEED_BYTES + 1)
            headers = getattr(response, "headers", None)
            content_encoding = ""
            if headers is not None:
                header_value = headers.get("Content-Encoding")
                if isinstance(header_value, str):
                    content_encoding = header_value.strip().casefold()
        if len(payload) > _MAX_FEED_BYTES:
            raise ValueError("feed payload exceeds 10 MiB limit")
        decoded = _decode_feed_payload(bytes(payload), content_encoding=content_encoding)
    except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
        _attach_fetch_failure_observation(
            exc,
            duration_ms=_elapsed_ms(started_at),
            redirect_count=redirect_counter.redirect_count,
        )
        raise
    return _ObservedFeedPayload(
        decoded,
        rss_fetch_duration_ms=_elapsed_ms(started_at),
        rss_redirect_count=redirect_counter.redirect_count,
    )


def _decode_feed_payload(payload: bytes, *, content_encoding: str = "") -> bytes:
    encoded_as_gzip = content_encoding == "gzip" or payload.startswith(_GZIP_MAGIC)
    if not encoded_as_gzip:
        return payload

    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as compressed:
            decoded = compressed.read(_MAX_FEED_BYTES + 1)
    except OSError as exc:
        raise ValueError("feed payload contained invalid gzip data") from exc
    if len(decoded) > _MAX_FEED_BYTES:
        raise ValueError("decoded feed payload exceeds 10 MiB limit")
    return bytes(decoded)


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
        return SourceCollectionResult(
            source.id,
            error="required source has no endpoint_url",
            collection_diagnostics=CollectionDiagnostics(failure_reason="missing_endpoint"),
        )

    try:
        payload = fetcher(source.endpoint_url)
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        ET.ParseError,
        ValueError,
        KeyError,
    ) as exc:
        detail = str(exc).strip() or type(exc).__name__
        return SourceCollectionResult(
            source.id,
            error=detail,
            collection_diagnostics=_fetch_error_diagnostics(exc),
        )

    try:
        normalized = _normalize_rss_source_observed(source, payload)
    except ET.ParseError as exc:
        detail = str(exc).strip() or type(exc).__name__
        return SourceCollectionResult(
            source.id,
            error=detail,
            collection_diagnostics=_merge_fetch_observation(
                CollectionDiagnostics(failure_reason="xml_parse_error"), payload
            ),
        )
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        ValueError,
        KeyError,
    ) as exc:
        detail = str(exc).strip() or type(exc).__name__
        return SourceCollectionResult(
            source.id,
            error=detail,
            collection_diagnostics=_merge_fetch_observation(
                CollectionDiagnostics(failure_reason="normalization_error"), payload
            ),
        )

    diagnostics = _merge_fetch_observation(normalized.diagnostics, payload)
    if normalized.error is not None:
        detail = str(normalized.error).strip() or type(normalized.error).__name__
        return SourceCollectionResult(
            source.id,
            error=detail,
            collection_diagnostics=diagnostics,
        )
    return SourceCollectionResult(
        source.id,
        editions=normalized.editions,
        collection_diagnostics=diagnostics,
    )


def _elapsed_ms(started_at: float) -> int:
    return max(round((time.monotonic() - started_at) * 1000), 0)


def _attach_fetch_failure_observation(
    exc: BaseException,
    *,
    duration_ms: int,
    redirect_count: int,
) -> None:
    setattr(exc, _FETCH_DURATION_ATTR, duration_ms)
    setattr(exc, _FETCH_REDIRECT_ATTR, max(redirect_count, 0))
    setattr(exc, _FETCH_FAILURE_KIND_ATTR, _classify_fetch_failure(exc))


def _classify_fetch_failure(exc: BaseException) -> RssFetchFailureKind:
    if isinstance(exc, urllib.error.HTTPError):
        if 300 <= exc.code < 400:
            return "redirect_error"
        if 400 <= exc.code < 500:
            return "http_4xx"
        if 500 <= exc.code < 600:
            return "http_5xx"
        return "other_io"

    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, ssl.SSLError):
        return "tls"
    if isinstance(reason, TimeoutError):
        return "timeout"
    if isinstance(reason, ConnectionError):
        return "connection"
    if isinstance(reason, ValueError):
        detail = str(reason)
        if detail in {
            "feed payload exceeds 10 MiB limit",
            "decoded feed payload exceeds 10 MiB limit",
        }:
            return "payload_too_large"
        if detail == "feed payload contained invalid gzip data":
            return "invalid_gzip"
    return "other_io"


def _fetch_error_diagnostics(exc: BaseException) -> CollectionDiagnostics:
    return CollectionDiagnostics(
        rss_fetch_duration_ms=_observed_nonnegative_int(exc, _FETCH_DURATION_ATTR),
        rss_fetch_failure_kind=_observed_failure_kind(exc),
        rss_redirect_count=_observed_nonnegative_int(exc, _FETCH_REDIRECT_ATTR),
        failure_reason="fetch_error",
    )


def _merge_fetch_observation(
    diagnostics: CollectionDiagnostics,
    payload: bytes,
) -> CollectionDiagnostics:
    if not isinstance(payload, _ObservedFeedPayload):
        return diagnostics
    return replace(
        diagnostics,
        rss_fetch_duration_ms=payload.rss_fetch_duration_ms,
        rss_redirect_count=payload.rss_redirect_count,
    )


def _observed_nonnegative_int(exc: BaseException, attribute: str) -> int | None:
    value = getattr(exc, attribute, None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _observed_failure_kind(exc: BaseException) -> RssFetchFailureKind | None:
    value = getattr(exc, _FETCH_FAILURE_KIND_ATTR, None)
    if not isinstance(value, str) or value not in _RSS_FETCH_FAILURE_KINDS:
        return None
    return cast(RssFetchFailureKind, value)


def normalize_rss_source(
    source: SourceDefinition,
    payload: bytes,
) -> tuple[CanonicalEdition, ...]:
    """Normalize one RSS payload into source-native canonical bulletin editions."""

    normalized = _normalize_rss_source_observed(source, payload)
    if normalized.error is not None:
        raise normalized.error
    return normalized.editions


def _normalize_rss_source_observed(
    source: SourceDefinition,
    payload: bytes,
) -> _NormalizedSource:
    root = ET.fromstring(payload)
    items = [element for element in root.iter() if _local_name(element.tag) == "item"]
    item_count = len(items)
    if not items:
        error = ValueError("feed contained no RSS items")
        return _NormalizedSource(
            (),
            CollectionDiagnostics(
                rss_item_count=0,
                rss_missing_required_fields_count=0,
                rss_filter_rejected_count=0,
                rss_duplicate_native_id_count=0,
                rss_invalid_published_at_count=0,
                rss_title_parser_attempted_count=0,
                rss_title_parser_exact_count=0,
                rss_title_parser_recovered_count=0,
                rss_title_parser_normalized_recovered_count=0,
                rss_title_parser_provider_compat_recovered_count=0,
                rss_title_parser_rejected_count=0,
                rss_native_id_guid_count=0,
                rss_native_id_enclosure_count=0,
                rss_native_id_link_count=0,
                failure_reason="no_rss_items",
            ),
            error,
        )

    parser_id = str(source.parser_id)
    try:
        parser = (
            None
            if parser_id == RELEASE_DATE_TITLE_PARSER_ID
            else get_recovery_title_parser(parser_id)
        )
        collection_filter = (
            None
            if source.collection_filter_id is None
            else get_collection_filter(source.collection_filter_id)
        )
    except KeyError as exc:
        return _NormalizedSource(
            (),
            CollectionDiagnostics(
                rss_item_count=item_count,
                failure_reason="configuration_error",
            ),
            exc,
        )
    except (OSError, TimeoutError, urllib.error.URLError, ET.ParseError, ValueError) as exc:
        return _NormalizedSource(
            (),
            CollectionDiagnostics(
                rss_item_count=item_count,
                failure_reason="normalization_error",
            ),
            exc,
        )

    editions: list[CanonicalEdition] = []
    seen_native_ids: set[str] = set()
    missing_required_fields_count = 0
    filter_rejected_count = 0
    duplicate_native_id_count = 0
    invalid_published_at_count = 0
    title_parser_attempted_count = 0
    title_parser_exact_count = 0
    title_parser_normalized_recovered_count = 0
    title_parser_provider_compat_recovered_count = 0
    title_parser_rejected_count = 0
    native_id_guid_count = 0
    native_id_enclosure_count = 0
    native_id_link_count = 0

    try:
        for item in items:
            title = _child_text(item, "title")
            source_native_id, native_id_origin = _source_native_id_with_origin(item)
            if native_id_origin == "guid":
                native_id_guid_count += 1
            elif native_id_origin == "enclosure":
                native_id_enclosure_count += 1
            elif native_id_origin == "link":
                native_id_link_count += 1

            published_text = _first_child_text(item, ("pubdate", "published", "date"))
            if title is None or source_native_id is None or published_text is None:
                missing_required_fields_count += 1
                continue
            if collection_filter is not None and not collection_filter.accepts(title):
                filter_rejected_count += 1
                continue
            if source_native_id in seen_native_ids:
                duplicate_native_id_count += 1
                continue

            try:
                published_at = _parse_published_at(published_text, source.timezone)
            except ValueError:
                invalid_published_at_count += 1
                continue

            edition_at = None
            if parser is not None:
                title_parser_attempted_count += 1
                attempt = parser.parse_with_recovery(title)
                parsed = attempt.parsed
                if parsed is None:
                    title_parser_rejected_count += 1
                    continue
                if attempt.mode == "exact":
                    title_parser_exact_count += 1
                elif attempt.mode == "normalized":
                    title_parser_normalized_recovered_count += 1
                elif attempt.mode == "provider_compat":
                    title_parser_provider_compat_recovered_count += 1
                edition_at = _apply_source_timezone(parsed, source.timezone)

            editions.append(
                CanonicalEdition(
                    source_id=source.id,
                    source_native_id=source_native_id,
                    title=title,
                    published_at=published_at,
                    edition_at=edition_at,
                    duration_seconds=_duration_seconds(item),
                )
            )
            seen_native_ids.add(source_native_id)
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        ET.ParseError,
        ValueError,
        KeyError,
    ) as exc:
        recovered_count = (
            title_parser_normalized_recovered_count + title_parser_provider_compat_recovered_count
        )
        return _NormalizedSource(
            (),
            CollectionDiagnostics(
                rss_item_count=item_count,
                rss_missing_required_fields_count=missing_required_fields_count,
                rss_filter_rejected_count=filter_rejected_count,
                rss_duplicate_native_id_count=duplicate_native_id_count,
                rss_invalid_published_at_count=invalid_published_at_count,
                rss_title_parser_attempted_count=(
                    title_parser_attempted_count if parser is not None else None
                ),
                rss_title_parser_exact_count=(
                    title_parser_exact_count if parser is not None else None
                ),
                rss_title_parser_recovered_count=(recovered_count if parser is not None else None),
                rss_title_parser_normalized_recovered_count=(
                    title_parser_normalized_recovered_count if parser is not None else None
                ),
                rss_title_parser_provider_compat_recovered_count=(
                    title_parser_provider_compat_recovered_count if parser is not None else None
                ),
                rss_title_parser_rejected_count=(
                    title_parser_rejected_count if parser is not None else None
                ),
                rss_native_id_guid_count=native_id_guid_count,
                rss_native_id_enclosure_count=native_id_enclosure_count,
                rss_native_id_link_count=native_id_link_count,
                failure_reason="normalization_error",
            ),
            exc,
        )

    recovered_count = (
        title_parser_normalized_recovered_count + title_parser_provider_compat_recovered_count
    )
    diagnostics = CollectionDiagnostics(
        rss_item_count=item_count,
        rss_missing_required_fields_count=missing_required_fields_count,
        rss_filter_rejected_count=filter_rejected_count,
        rss_duplicate_native_id_count=duplicate_native_id_count,
        rss_invalid_published_at_count=invalid_published_at_count,
        rss_title_parser_attempted_count=(
            title_parser_attempted_count if parser is not None else None
        ),
        rss_title_parser_exact_count=(title_parser_exact_count if parser is not None else None),
        rss_title_parser_recovered_count=(recovered_count if parser is not None else None),
        rss_title_parser_normalized_recovered_count=(
            title_parser_normalized_recovered_count if parser is not None else None
        ),
        rss_title_parser_provider_compat_recovered_count=(
            title_parser_provider_compat_recovered_count if parser is not None else None
        ),
        rss_title_parser_rejected_count=(
            title_parser_rejected_count if parser is not None else None
        ),
        rss_native_id_guid_count=native_id_guid_count,
        rss_native_id_enclosure_count=native_id_enclosure_count,
        rss_native_id_link_count=native_id_link_count,
    )

    accepted_count = title_parser_exact_count + recovered_count
    if parser is not None and _title_parser_drifted(
        attempted_count=title_parser_attempted_count,
        accepted_count=accepted_count,
    ):
        error = ValueError("title parser contract degraded")
        return _NormalizedSource(
            (),
            replace(diagnostics, failure_reason="title_parser_drift"),
            error,
        )

    if not editions:
        error = ValueError("feed contained no canonical bulletin editions")
        return _NormalizedSource(
            (),
            replace(diagnostics, failure_reason="no_canonical_editions"),
            error,
        )
    return _NormalizedSource(tuple(editions), diagnostics)


def _title_parser_drifted(*, attempted_count: int, accepted_count: int) -> bool:
    if attempted_count < _TITLE_PARSE_MIN_SAMPLE:
        return False
    return accepted_count / attempted_count < _TITLE_PARSE_MIN_ACCEPT_RATIO


def _source_native_id(item: ET.Element) -> str | None:
    value, _origin = _source_native_id_with_origin(item)
    return value


def _source_native_id_with_origin(item: ET.Element) -> tuple[str | None, str | None]:
    for name in ("guid", "id"):
        value = _child_text(item, name)
        if value is not None:
            return value, "guid"

    enclosure = _child(item, "enclosure")
    if enclosure is not None:
        value = _nonempty(enclosure.attrib.get("url"))
        if value is not None:
            return value, "enclosure"

    link = _child(item, "link")
    if link is not None:
        value = _element_text(link) or _nonempty(link.attrib.get("href"))
        if value is not None:
            return value, "link"
    return None, None


def _parse_published_at(value: str, source_timezone: ZoneInfo) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except TypeError, ValueError:
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
