from __future__ import annotations

import html
import re
import sys
import time
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from math import ceil

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.collection import fetch_feed as fetch_runtime_feed
from news_bulletin_playlist.collection import normalize_rss_source
from news_bulletin_playlist.models import SourceDefinition
from news_bulletin_playlist.providers.base import TitleParser
from news_bulletin_playlist.registry import CORE_PROVIDERS, ProviderConfig

_SAMPLE_SIZE = 6
_MIN_PARSE_RATIO = 0.5
_RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})
_RETRY_DELAYS_SECONDS = (0.0, 2.0, 5.0)
_ONDACERO_HTML_TITLE = re.compile(
    r"Las noticias de Onda Cero de las\s+"
    r"(?:[01]?\d|2[0-3]):[0-5]\d[hH]\s*"
    r"\(\d{1,2}/\d{1,2}/\d{4}\)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ContractResult:
    provider_id: str
    ok: bool
    sampled: int
    parsed: int
    detail: str


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
    page = html.unescape(payload.decode("utf-8", errors="replace"))
    if provider_id != "ondacero":
        return []
    return list(dict.fromkeys(match.group(0) for match in _ONDACERO_HTML_TITLE.finditer(page)))


def evaluate_titles(provider_id: str, parser: TitleParser, titles: list[str]) -> ContractResult:
    sample = titles[:_SAMPLE_SIZE]
    if not sample:
        return ContractResult(provider_id, False, 0, 0, "source contained no bulletin titles")

    parsed = sum(parser.parse(title) is not None for title in sample)
    required = max(1, ceil(len(sample) * _MIN_PARSE_RATIO))
    ok = parsed >= required
    detail = f"parsed {parsed}/{len(sample)} recent titles; required >= {required}"
    if not ok:
        examples = " | ".join(sample[:3])
        detail = f"{detail}; examples: {examples}"
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
