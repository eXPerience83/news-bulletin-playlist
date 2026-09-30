"""Frozen pre-#441 observations plus a reproducible 6,960-title boundary matrix."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from _temporal_corpus import temporal_matrix

from news_bulletin_playlist.models import TemporalStrategy
from news_bulletin_playlist.providers.base import CatalogueParseContext, parse_title
from news_bulletin_playlist.providers.temporal_detector import TemporalSample
from news_bulletin_playlist.registry import get_temporal_detector, get_title_parser

FIXTURES = Path(__file__).parent / "fixtures" / "temporal"
HISTORICAL = json.loads((FIXTURES / "historical_titles.json").read_text())


def stamp(attempt):
    return None if attempt.parsed is None else attempt.parsed.edition_at.astimezone(UTC).isoformat()


@pytest.mark.parametrize("row", HISTORICAL, ids=lambda row: row["title"])
def test_frozen_historical_titles(row):
    context = CatalogueParseContext(row["day"], "day", ZoneInfo(row["zone"]))
    legacy = parse_title(get_title_parser(row["parser"]), row["title"], context=context)
    strategy = (
        TemporalStrategy.CLOCK_CATALOGUE_DAY
        if row["parser"] == "rmf_fakty"
        else TemporalStrategy.FULL_DATETIME
    )
    universal = get_temporal_detector((), strategy).parse(row["title"], context=context)
    assert stamp(legacy) == row["expected"]
    # A configured clock capability attests the field, independently of the product name.
    expected = (
        "2026-09-28T14:00:00+00:00" if row["title"] == "16:00 Faktycznie" else row["expected"]
    )
    assert stamp(universal) == expected


def test_differential_matrix_matches_frozen_original_and_runtime():
    baseline = json.loads((FIXTURES / "matrix_baseline.json").read_text())
    matrix = temporal_matrix()
    assert len(matrix) == baseline["count"]
    digest = hashlib.sha256()
    detector = get_temporal_detector((), TemporalStrategy.FULL_DATETIME)
    improvements = 0
    for case in matrix:
        context = CatalogueParseContext(case.day, "day", ZoneInfo(case.zone))
        legacy = parse_title(get_title_parser(case.parser), case.title, context=context)
        expected = stamp(legacy)
        digest.update(
            json.dumps(
                [case.parser, case.title, case.day, case.zone, expected], ensure_ascii=False
            ).encode()
            + b"\n"
        )
        universal = detector.parse(case.title, context=context)
        if stamp(universal) != expected:
            # Reviewed improvement: pipe-separated compact dates/clocks and bare hours
            # previously required a hyphen in the product-specific parser.
            assert case.parser == "rne" and expected is None, case
            assert universal.parsed is not None
            assert universal.parsed.edition_at.date().isoformat() == case.day
            assert " | " in case.title
            clock_field = case.title.split(" | ")[1].rstrip("H. ")
            assert clock_field in {"19", "19.30", "19,30", "1930", "930"}
            assert (universal.parsed.edition_at.hour, universal.parsed.edition_at.minute) == (
                9 if clock_field == "930" else 19,
                0 if clock_field == "19" else 30,
            )
            improvements += 1
    assert improvements == 192
    # This oracle was recorded with the unmodified base revision, not the shared resolver.
    assert digest.hexdigest() == baseline["sha256"]


@pytest.mark.parametrize(
    "row",
    json.loads((FIXTURES / "intentional_changes.json").read_text()),
    ids=lambda row: row["classification"] + ": " + row["title"],
)
def test_every_targeted_semantic_difference_is_explicit(row):
    context = CatalogueParseContext(
        row["day"], "day", ZoneInfo(row["zone"]) if row["zone"] else None
    )
    samples = ()
    strategy = (
        TemporalStrategy.CLOCK_CATALOGUE_DAY
        if row["parser"] == "rmf_fakty"
        else TemporalStrategy.FULL_DATETIME
    )
    expected = row["expected"]
    if row["parser"] == "release_date_title":
        strategy = TemporalStrategy.AUTO
        if row["title"].startswith("Die Nachrichten"):
            samples = tuple(
                TemporalSample(row["title"].replace("21:00", f"{hour}:00"), context)
                for hour in range(17, 22)
            )
        else:
            # Corrected cross-contract contamination: a single foreign layout proves nothing.
            expected = None
    assert (
        stamp(get_temporal_detector(samples, strategy).parse(row["title"], context=context))
        == expected
    )
