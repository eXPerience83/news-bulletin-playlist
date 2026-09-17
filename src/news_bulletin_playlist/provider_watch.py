from __future__ import annotations

import sys
import time
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from math import ceil

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.collection import fetch_feed as fetch_runtime_feed
from news_bulletin_playlist.collection import normalize_rss_source
from news_bulletin_playlist.models import SourceDefinition
from news_bulletin_playlist.providers.base import RecoveryTitleParser
from news_bulletin_playlist.providers.title_recovery import normalize_title_syntax
from news_bulletin_playlist.registry import CORE_PROVIDERS, ProviderConfig

_SAMPLE_SIZE = 6
_MIN_PARSE_RATIO = 0.5
_RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})
_RETRY_DELAYS_SECONDS = (0.0, 2.0, 5.0)
_ONDACERO_TITLE_PREFIX = "las noticias de onda cero"
_FALLBACK_TEXT_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "a", "p", "span", "li"})


@dataclass(frozen=True, slots=True)
class ContractResult:
    provider_id: str
    ok: bool
    sampled: int
    parsed: int
    detail: str


class _FallbackTitleHTMLParser(HTMLParser):
    """Extract complete text units; never detach a bulletin-looking substring."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._captures: list[tuple[str, list[str]]] = []
        self.titles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.casefold()
        if normalized in _FALLBACK_TEXT_TAGS:
            self._captures.append((normalized, []))

    def handle_data(self, data: str) -> None:
        for _tag, parts in self._captures:
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        for index in range(len(self._captures) - 1, -1, -1):
            capture_tag, parts = self._captures[index]
            if capture_tag != normalized:
                continue
            self._captures.pop(index)
            # Nested supported tags are formatting fragments of the enclosing title unit.
            # Their text is already accumulated into every active ancestor capture, so emitting
            # the nested fragment independently could make a promotional/suffixed headline pass
            # the provider-watch ratio using text that never existed as a complete title.
            if self._captures:
                break
            text = " ".join("".join(parts).split()).strip()
            if text:
                self.titles.append(text)
            break


def extract_rss_titles(payload: bytes) -> list[str]:
    root = ET.fromstring(payload)
    titles: list[str] = []
    for item in root.iter():
        if _local_name(item.tag) != "item":
            continue
        for child in item:
            if _local_name(child.tag) == "title" and child.text:
                title = child.text.strip()
                if title:
                    titles.append(title)
                break
    return titles


def extract_fallback_titles(provider_id: str, payload: bytes) -> list[str]:
    if provider_id != "ondacero":
        return []
    parser = _FallbackTitleHTMLParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    candidates = (
        title
        for title in parser.titles
        if normalize_title_syntax(title).casefold().startswith(_ONDACERO_TITLE_PREFIX)
    )
    return list(dict.fromkeys(candidates))


def evaluate_titles(
    provider_id: str,
    parser: RecoveryTitleParser,
    titles: list[str],
) -> ContractResult:
    sample = titles[:_SAMPLE_SIZE]
    if not sample:
        return ContractResult(provider_id, False, 0, 0, "source contained no bulletin titles")

    parsed = 0
    recovered = 0
    for title in sample:
        attempt = parser.parse_with_recovery(title)
        accepted = attempt.parsed is not None
        parsed += int(accepted)
        recovered += int(accepted and attempt.mode in {"normalized", "provider_compat"})

    required = max(1, ceil(len(sample) * _MIN_PARSE_RATIO))
    ok = parsed >= required
    detail = f"parsed {parsed}/{len(sample)} recent titles; required >= {required}"
    if recovered:
        detail = f"{detail}; recovered {recovered}/{len(sample)} with safe title compatibility"
    if not ok:
        detail = f"{detail}; unresolved {len(sample) - parsed}/{len(sample)}"
    return ContractResult(provider_id, ok, len(sample), parsed, detail)


def fetch_feed_once(url: str, timeout: float = 20.0) -> bytes:
    # Use the exact bounded/decompression path used by the runtime so the watchdog catches
    # operational incompatibilities such as gzip RSS responses before deployment.
    return fetch_runtime_feed(url, timeout=timeout)


def fetch_feed(url: str, timeout: float = 20.0) -> bytes:
    last_error: BaseException | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS_SECONDS, start=1):
        if delay:
            time.sleep(delay)
        try:
            return fetch_feed_once(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in _RETRYABLE_HTTP or attempt == len(_RETRY_DELAYS_SECONDS):
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == len(_RETRY_DELAYS_SECONDS):
                raise
    raise RuntimeError(f"feed retries exhausted: {last_error}")


def check_provider(provider: ProviderConfig) -> ContractResult:
    try:
        payload = fetch_feed(provider.feed_url)
        titles = extract_rss_titles(payload)
    except (urllib.error.URLError, TimeoutError, ET.ParseError, OSError, ValueError) as feed_error:
        if provider.contract_fallback_url is None:
            return ContractResult(
                provider.provider_id,
                False,
                0,
                0,
                f"feed unavailable or invalid after retries: {feed_error}",
            )
        try:
            fallback_payload = fetch_feed(provider.contract_fallback_url)
            titles = extract_fallback_titles(provider.provider_id, fallback_payload)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as fallback_error:
            return ContractResult(
                provider.provider_id,
                False,
                0,
                0,
                f"feed failed ({feed_error}); fallback failed ({fallback_error})",
            )
        result = evaluate_titles(provider.provider_id, provider.parser, titles)
        return ContractResult(
            result.provider_id,
            result.ok,
            result.sampled,
            result.parsed,
            f"{result.detail}; checked official fallback page after RSS failure",
        )
    return evaluate_titles(provider.provider_id, provider.parser, titles)


def check_catalog_source(source: SourceDefinition) -> ContractResult:
    provider_id = f"catalog:{source.id}"
    endpoint = source.endpoint_url
    if endpoint is None:
        return ContractResult(provider_id, False, 0, 0, "built-in source has no endpoint")
    try:
        payload = fetch_feed(endpoint)
        editions = normalize_rss_source(source, payload)
    except (
        urllib.error.URLError,
        TimeoutError,
        ET.ParseError,
        OSError,
        ValueError,
        KeyError,
    ) as exc:
        return ContractResult(
            provider_id,
            False,
            0,
            0,
            f"runtime collection contract failed: {exc}",
        )

    sampled = min(len(editions), _SAMPLE_SIZE)
    return ContractResult(
        provider_id,
        True,
        sampled,
        sampled,
        f"runtime normalized {len(editions)} canonical editions",
    )


def format_report(results: list[ContractResult]) -> str:
    lines = ["# Provider contract watch", ""]
    for result in results:
        marker = "OK" if result.ok else "FAIL"
        lines.append(f"- **{result.provider_id}** — {marker}: {result.detail}")
    return "\n".join(lines)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def main() -> int:
    # Keep the historical parser-shape checks and also run the actual runtime normalization
    # contract for every selectable built-in source, including newly added international feeds.
    results = [check_provider(provider) for provider in CORE_PROVIDERS]
    results.extend(check_catalog_source(source) for source in BUILTIN_CATALOG.sources)
    print(format_report(results))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
