"""Frozen pre-#441 observations plus a reproducible 6,960-title boundary matrix."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from _temporal_corpus import temporal_matrix

from news_bulletin_playlist.providers.base import CatalogueParseContext, parse_title
from news_bulletin_playlist.registry import get_temporal_detector, get_title_parser

FIXTURES = Path(__file__).parent / "fixtures" / "temporal"
HISTORICAL = json.loads((FIXTURES / "historical_titles.json").read_text())


def stamp(attempt):
    return None if attempt.parsed is None else attempt.parsed.edition_at.astimezone(UTC).isoformat()


@pytest.mark.parametrize("row", HISTORICAL, ids=lambda row: row["title"])
def test_frozen_historical_titles(row):
    context = CatalogueParseContext(row["day"], "day", ZoneInfo(row["zone"]))
    legacy = parse_title(get_title_parser(row["parser"]), row["title"], context=context)
    universal = get_temporal_detector(()).parse(row["title"], context=context)
    assert stamp(legacy) == row["expected"]
    assert stamp(universal) == row["expected"]


def test_differential_matrix_matches_frozen_original_and_runtime():
    baseline = json.loads((FIXTURES / "matrix_baseline.json").read_text())
    matrix = temporal_matrix()
    assert len(matrix) == baseline["count"]
    digest = hashlib.sha256()
    detector = get_temporal_detector(())
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
        assert stamp(universal) == expected, case
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
    assert row["before"] != row["expected"]
    assert stamp(get_temporal_detector(()).parse(row["title"], context=context)) == row["expected"]
