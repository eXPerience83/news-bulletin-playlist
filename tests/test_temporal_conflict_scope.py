from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.models import TemporalStrategy
from news_bulletin_playlist.providers.base import CatalogueParseContext
from news_bulletin_playlist.providers.temporal_detector import TemporalSample
from news_bulletin_playlist.registry import get_temporal_detector

MADRID = ZoneInfo("Europe/Madrid")


def _context(day: str) -> CatalogueParseContext:
    return CatalogueParseContext(day, "day", MADRID)


def _sample(day: str, hour: int) -> TemporalSample:
    return TemporalSample(
        f"NOTICIAS RNE - {datetime.fromisoformat(day).strftime('%d.%m.%Y')} - {hour:02}.00H",
        _context(day),
    )


def test_signature_conflict_blocks_auto_but_not_valid_configured_full_datetime() -> None:
    valid = tuple(_sample("2026-10-03", hour) for hour in (9, 10, 11, 12, 15))
    conflict = TemporalSample(
        "NOTICIAS RNE - 02.10.2026 - 17.00H",
        _context("2026-10-03"),
    )
    corpus = (*valid, conflict)

    auto = get_temporal_detector(corpus, TemporalStrategy.AUTO)
    assert auto.conflicts == 1
    assert auto.parse(valid[0].title, context=valid[0].context).parsed is None

    configured = get_temporal_detector(corpus, TemporalStrategy.FULL_DATETIME)
    valid_attempt = configured.parse(valid[0].title, context=valid[0].context)
    assert valid_attempt.parsed is not None
    assert valid_attempt.temporal_evidence == "configured_capability"
    assert valid_attempt.parsed.edition_at.astimezone(UTC) == datetime(2026, 10, 3, 7, tzinfo=UTC)

    conflict_attempt = configured.parse(conflict.title, context=conflict.context)
    assert conflict_attempt.parsed is None
    assert conflict_attempt.temporal_evidence == "catalogue_date_conflict"


def test_configured_full_datetime_still_rejects_row_local_period_conflict() -> None:
    valid = tuple(_sample("2026-10-03", hour) for hour in (9, 10, 11, 12, 15))
    conflict = TemporalSample(
        "NOTICIAS RNE - 02.10.2026 - 17.00H",
        _context("2026-10-03"),
    )
    detector = get_temporal_detector((*valid, conflict), TemporalStrategy.FULL_DATETIME)

    attempt = detector.parse(
        "NOTICIAS RNE - 04.10.2026 - 13.00H",
        context=_context("2026-10-03"),
    )
    assert attempt.parsed is None
    assert attempt.temporal_evidence == "catalogue_date_conflict"


COMPLETE_DATE_STRATEGIES = (
    TemporalStrategy.FULL_DATETIME,
    TemporalStrategy.PARTIAL_DATE_CATALOGUE_DAY,
    TemporalStrategy.EXPLICIT_UTC,
)


@pytest.mark.parametrize("strategy", COMPLETE_DATE_STRATEGIES)
@pytest.mark.parametrize("precision,release", [("month", "2026-09"), ("year", "2026")])
def test_coarse_complete_calendar_survives_sibling_conflict(strategy, precision, release):
    good = TemporalSample(
        "Edition 28/09/2026 - 12:30 UTC",
        CatalogueParseContext(release, precision, ZoneInfo("UTC")),
    )
    bad = TemporalSample(
        "Edition 29/09/2026 - 13:30 UTC",
        CatalogueParseContext("2026-09-28", "day", ZoneInfo("UTC")),
    )
    expected = datetime(2026, 9, 28, 12, 30, tzinfo=UTC)
    alone = get_temporal_detector((good,), strategy).parse(good.title, context=good.context)
    assert alone.parsed is not None
    assert alone.parsed.edition_at.astimezone(UTC) == expected

    for corpus in ((good, bad), (bad, good)):
        detector = get_temporal_detector(corpus, strategy)
        assert detector.conflicts == 1
        attempt = detector.parse(good.title, context=good.context)
        assert attempt.parsed is not None
        assert attempt.temporal_evidence == "configured_capability"
        assert attempt.parsed.edition_at.astimezone(UTC) == expected
        rejected = detector.parse(bad.title, context=bad.context)
        assert rejected.parsed is None
        assert rejected.temporal_evidence == "catalogue_date_conflict"
        # The coarse witness proves DMY; MDY cannot be substituted to fit a day.
        assert (
            detector.parse(
                "Edition 09/10/2026 - 12:30 UTC",
                context=CatalogueParseContext("2026-09-10", "day", ZoneInfo("UTC")),
            ).parsed
            is None
        )
        # An explicit UTC label never overrides a nonzero source-local offset.
        assert (
            detector.parse(
                good.title, context=CatalogueParseContext(release, precision, MADRID)
            ).parsed
            is None
        )
        auto = get_temporal_detector(corpus, TemporalStrategy.AUTO)
        assert auto.supporting_rows == 0
        assert auto.parse(good.title, context=good.context).parsed is None


@pytest.mark.parametrize("strategy", COMPLETE_DATE_STRATEGIES)
def test_ambiguous_year_calendar_cannot_supply_an_order_witness(strategy):
    context = CatalogueParseContext("2026", "year", ZoneInfo("UTC"))
    ambiguous = TemporalSample("Edition 09/10/2026 - 12:30 UTC", context)
    assert (
        get_temporal_detector((ambiguous,), strategy).parse(ambiguous.title, context=context).parsed
        is None
    )
    bad = TemporalSample(
        "Edition 29/09/2026 - 13:30 UTC",
        CatalogueParseContext("2026-09-28", "day", ZoneInfo("UTC")),
    )
    for corpus in ((ambiguous, bad), (bad, ambiguous)):
        detector = get_temporal_detector(corpus, strategy)
        assert detector.conflicts == 2
        for sample in corpus:
            attempt = detector.parse(sample.title, context=sample.context)
            assert attempt.parsed is None
            assert attempt.temporal_evidence == "catalogue_date_conflict"
        # An unresolved ambiguous row must not authorize another row of this layout.
        assert detector.parse("Edition 28/09/2026 - 12:30 UTC", context=context).parsed is None


@pytest.mark.parametrize("strategy", COMPLETE_DATE_STRATEGIES)
def test_incompatible_coarse_order_witnesses_keep_signature_fail_closed(strategy):
    context = CatalogueParseContext("2026-09", "month", ZoneInfo("UTC"))
    dmy = TemporalSample("Edition 28/09/2026 - 12:30 UTC", context)
    mdy = TemporalSample("Edition 09/28/2026 - 13:30 UTC", context)
    bad = TemporalSample(
        "Edition 29/09/2026 - 14:30 UTC",
        CatalogueParseContext("2026-09-28", "day", ZoneInfo("UTC")),
    )
    for sample in (dmy, mdy):
        assert (
            get_temporal_detector((sample,), strategy)
            .parse(sample.title, context=sample.context)
            .parsed
            is not None
        )
    for corpus in ((dmy, mdy, bad), (bad, mdy, dmy)):
        detector = get_temporal_detector(corpus, strategy)
        assert detector.conflicts == 1
        for sample in corpus:
            attempt = detector.parse(sample.title, context=sample.context)
            assert attempt.parsed is None
            assert attempt.temporal_evidence == "catalogue_date_conflict"


@pytest.mark.parametrize("precision,release", [("month", "2026-09"), ("year", "2026")])
def test_coarse_order_witnesses_do_not_train_auto(precision, release):
    context = CatalogueParseContext(release, precision, ZoneInfo("UTC"))
    corpus = tuple(
        TemporalSample(f"Edition 28/09/2026 - {hour}:30 UTC", context) for hour in range(10, 15)
    )
    detector = get_temporal_detector(corpus, TemporalStrategy.AUTO)
    assert detector.conflicts == 0
    assert detector.supporting_rows == 0
    assert all(detector.parse(row.title, context=row.context).parsed is None for row in corpus)


def test_coarse_order_witness_does_not_change_auto_learned_orders_or_support():
    day = CatalogueParseContext("2026-09-28", "day", ZoneInfo("UTC"))
    auto_valid = tuple(
        TemporalSample(f"Edition 28/09/2026 - {hour}:30 UTC", day) for hour in range(10, 15)
    )
    coarse_mdy = TemporalSample(
        "Edition 09/28/2026 - 15:30 UTC",
        CatalogueParseContext("2026-09", "month", ZoneInfo("UTC")),
    )
    detector = get_temporal_detector((*auto_valid, coarse_mdy), TemporalStrategy.AUTO)
    assert detector.conflicts == 0
    assert detector.supporting_rows == 5
    for sample in auto_valid:
        attempt = detector.parse(sample.title, context=sample.context)
        assert attempt.parsed is not None
        assert attempt.temporal_evidence == "catalogue_structure"
        assert attempt.supporting_rows == 5
    assert detector.parse(coarse_mdy.title, context=coarse_mdy.context).parsed is None
