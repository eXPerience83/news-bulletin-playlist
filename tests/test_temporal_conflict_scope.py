from datetime import UTC, datetime
from zoneinfo import ZoneInfo

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
