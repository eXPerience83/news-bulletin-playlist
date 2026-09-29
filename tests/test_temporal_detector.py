from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.models import ParserId, SourceId
from news_bulletin_playlist.providers.base import CatalogueParseContext
from news_bulletin_playlist.providers.temporal_detector import TemporalSample
from news_bulletin_playlist.registry import get_temporal_detector
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.first_models import TimestampSource, episode_order_key


def context(day="2026-09-28", zone="Europe/Madrid", precision="day"):
    return CatalogueParseContext(day, precision, ZoneInfo(zone) if zone else None)


def parse(title, ctx=None, samples=()):
    return get_temporal_detector(samples).parse(title, context=ctx or context())


@pytest.mark.parametrize(
    "title,zone,expected,mode",
    [
        ("Die Nachrichten vom 28.09.2026, 21:00 Uhr", "Europe/Berlin", "19:00", "exact"),
        ("21:00 Fakty", "Europe/Warsaw", "19:00", "catalogue_context"),
        ("6.30 Fakty sportowe", "Europe/Warsaw", "04:30", "catalogue_context"),
        ("Las noticias de la SER, 21:00 (28/09/2026)", "Europe/Madrid", "19:00", "exact"),
        ("Las noticias de Onda Cero de las 21:00h (28/09/2026)", "Europe/Madrid", "19:00", "exact"),
        ("CNN 5 cosas 09/28/26 6am", "America/New_York", "10:00", "exact"),
        ("Journal 28/09/2026 21h00 GMT", "UTC", "21:00", "exact"),
        ("NOTICIAS RNE - 280926 - 1730H", "Europe/Madrid", "15:30", "exact"),
        ("21:00H | 28 SEP 2026 | BOLETÍN", "Europe/Madrid", "19:00", "exact"),
        ("Las noticias de la SER - 21.00 28-09-26", "Europe/Madrid", "19:00", "provider_compat"),
        ("NOTICIAS\u00a0RNE – 28.09.2026 – 17.30H", "Europe/Madrid", "15:30", "normalized"),
    ],
)
def test_reviewed_temporal_evidence(title, zone, expected, mode):
    attempt = parse(title, context(zone=zone))
    assert attempt.parsed is not None
    assert (
        attempt.parsed.edition_at.astimezone(UTC).isoformat() == f"2026-09-28T{expected}:00+00:00"
    )
    assert attempt.parsed.title == title
    assert attempt.mode == mode


@pytest.mark.parametrize(
    "title",
    [
        "Programa 2026 - entrevista a las 21:00",
        "Trump hablará a las 18:00, reunión a las 20:00 y rueda de prensa a las 21:00",
        "Reunión 18:00, debate 20:00, cierre 22:00",
        "Resultados 20-10, edición 2026",
        "UN News Today 28 September 2026",
        "Las noticias de la SER (28/09/2026)",
        "El día en dos minutos (28/09/2026)",
        "Titular sin ninguna fecha ni hora",
        "Expansión",
        "Reuters",
        "ABC",
        "DW Actualidad",
        "N+ Univision",
        "16:00 Completely unrelated podcast",
        "16:00",
        "NOTICIAS - RNE 2809206 - 1730H",
        "21:00 Fakty 18:00.",
        "21:00 Fakty 18:00,",
        "21:00 Fakty 18:00 Uhr",
        "21:00 Fakty 28/09/2026 29/09/2026",
        "21:00 Fakty 24:00",
        "21:00 Fakty 13pm",
        "Die Nachrichten vom 28.09.2026, 21:00 Uhr 22:00",
        "Die Nachrichten vom 28.09.2026, 21:00 Uhr 29.09.2026",
        "Die Nachrichten vom 28.09.2026, 24:00 Uhr",
        "Die Nachrichten vom ２８.09.2026, 21:00 Uhr",
        "Die Nachrichten vom 28.09.2026, ٢١:00 Uhr",
        "Las notİcias de la SER, 21:00 (28/09/2026)",
        "Las noticias de la SER, 21:00 (28∕09∕2026)",
        "Die Nachrichten vom 28.09.2026, 21:00 Uhr" + "X" * 1025,
    ],
)
def test_unsupported_or_conflicting_evidence_falls_back(title):
    assert parse(title).parsed is None


@pytest.mark.parametrize("zone", ["Europe/Madrid", "Europe/Berlin", "Europe/Warsaw"])
@pytest.mark.parametrize("day", ["2026-03-29", "2026-10-25"])
def test_european_dst_rejected_for_both_routes(zone, day):
    stamp = datetime.fromisoformat(day).strftime("%d.%m.%Y")
    for title in (f"Die Nachrichten vom {stamp}, 02:30 Uhr", "02:30 Fakty"):
        assert parse(title, context(day, zone)).parsed is None


@pytest.mark.parametrize("day,clock", [("2026-03-08", "2:30am"), ("2026-11-01", "1:30am")])
def test_american_dst_rejected(day, clock):
    stamp = datetime.fromisoformat(day).strftime("%m/%d/%Y")
    assert parse(f"CNN 5 cosas {stamp} {clock}", context(day, "America/New_York")).parsed is None


@pytest.mark.parametrize(
    "precision,release", [("day", "2026-09-29"), ("month", "2026-08"), ("year", "2025")]
)
def test_title_date_must_agree_with_official_period(precision, release):
    assert (
        parse(
            "Die Nachrichten vom 28.09.2026, 21:00 Uhr", context(release, precision=precision)
        ).parsed
        is None
    )


@pytest.mark.parametrize(
    "precision,release", [("day", "2026-09-28"), ("month", "2026-09"), ("year", "2026")]
)
def test_full_title_can_prove_time_with_coarse_catalogue(precision, release):
    assert (
        parse(
            "Die Nachrichten vom 28.09.2026, 21:00 Uhr", context(release, precision=precision)
        ).parsed
        is not None
    )


@pytest.mark.parametrize(
    "precision,release",
    [("day", "20260928"), ("day", "2026-02-30"), ("month", "2026-09"), ("year", "2026")],
)
def test_clock_only_requires_canonical_exact_day(precision, release):
    assert parse("21:00 Fakty", context(release, precision=precision)).parsed is None


def test_timezone_required_and_overrides_legacy_defaults():
    title = "Las noticias de la SER, 21:00 (28/09/2026)"
    assert parse(title, context(zone=None)).parsed is None
    attempt = parse(title, context(zone="America/New_York"))
    assert attempt.parsed.edition_at.astimezone(UTC) == datetime(2026, 9, 29, 1, tzinfo=UTC)
    assert parse("Journal 28/09/2026 21h00 gmt", context()).parsed is None


def samples(prefix="Bulletin", date_format="%d/%m/%Y", clock_format="%H:%M"):
    return tuple(
        TemporalSample(
            f"{prefix} {value.strftime(date_format)} - {value.strftime(clock_format)}",
            context(value.date().isoformat()),
        )
        for value in (
            datetime(2026, 9, day, hour, 30)
            for day, hour in [(26, 17), (26, 18), (27, 17), (27, 19), (28, 17)]
        )
    )


@pytest.mark.parametrize("date_format", ["%d/%m/%Y", "%m/%d/%y", "%Y-%m-%d", "%d%m%Y", "%d %B %Y"])
@pytest.mark.parametrize("clock_format", ["%H:%M", "%H.%M", "%H%MH", "%I:%M%p"])
def test_unknown_layout_can_be_proven_without_a_source_profile(date_format, clock_format):
    corpus = samples(date_format=date_format, clock_format=clock_format)
    detector = get_temporal_detector(corpus)
    for sample in corpus:
        attempt = detector.parse(sample.title, context=sample.context)
        assert attempt.parsed is not None, sample.title
        assert attempt.parsed.edition_at.date().isoformat() == sample.context.release_date
        assert attempt.mode == "exact"


def test_damaged_compact_date_uses_day_only_after_structure_is_proven():
    corpus = samples(prefix="NOTICIAS - RNE", date_format="%d%m%Y", clock_format="%H%MH")
    title = "NOTICIAS - RNE 2809206 - 1730H"
    damaged = TemporalSample(title, context())
    attempt = parse(title, samples=(*corpus, damaged))
    assert attempt.mode == "catalogue_context"
    assert attempt.parsed.edition_at.astimezone(UTC) == datetime(2026, 9, 28, 15, 30, tzinfo=UTC)
    assert parse(title).parsed is None
    assert parse(title, context("2026-09", precision="month"), corpus).parsed is None


def test_structural_activation_is_bounded_diverse_and_order_independent():
    corpus = samples()
    target = corpus[-1]
    for insufficient in (
        corpus[:4],
        corpus[:1] * 5,
        corpus * 41,
        (
            *corpus,
            TemporalSample("An unrelated title", context()),
            TemporalSample("Another unrelated title", context()),
        ),
    ):
        assert parse(target.title, target.context, insufficient).parsed is None
    forward = parse(target.title, target.context, corpus)
    reverse = parse(target.title, target.context, tuple(reversed(corpus)))
    assert forward.parsed == reverse.parsed


def test_contradiction_disables_inference_even_with_high_coverage():
    corpus = samples()
    conflict = TemporalSample("Bulletin 29/09/2026 - 21:30", context())
    assert parse(corpus[0].title, corpus[0].context, (*corpus, conflict)).parsed is None


def test_date_order_may_not_switch_to_make_a_contradiction_fit():
    corpus = samples()
    conflict = TemporalSample("Bulletin 09/28/2026 - 21:30", context())
    assert parse(corpus[0].title, corpus[0].context, (*corpus, conflict)).parsed is None


@pytest.mark.parametrize(
    "title",
    [
        "Bulletin 28/09/2026 - 17:30 18:30",
        "Bulletin 28/09/2026 - 17:30 29/09/2026",
        "Bulletin 28/09/2026 interview at 17:30",
        "Bulletin 28/09/2026 - 17:30 interview",
        "Bulletin 29/09/2026 - 17:30",
        "Bulletin 28/09/2026 - 24:30",
        "Bulletin 28/09/2026 - ١٧:30",
        "Bulletin 28/09/2026 - 17:30:00",
        "Bulletin 28/09/2026 - 17:30 (29/09/2026)",
    ],
)
def test_learned_layout_does_not_authorize_arbitrary_titles(title):
    assert parse(title, samples=samples()).parsed is None


class Client:
    def __init__(self, titles):
        self.calls = 0
        self.items = [
            {
                "id": str(index).zfill(22),
                "uri": "spotify:episode:" + str(index).zfill(22),
                "name": title,
                "release_date": "2026-09-28",
                "release_date_precision": "day",
            }
            for index, title in enumerate(titles)
        ]

    def show_episodes(self, show_id, *, limit=50, offset=0):
        self.calls += 1
        assert offset == 0
        return catalogue_page(self.items)


def catalogue(titles, zone="Europe/Berlin"):
    source = replace(
        BUILTIN_CATALOG.sources[0],
        id=SourceId("unknown_source"),
        parser_id=ParserId("release_date_title"),
        timezone=ZoneInfo(zone),
    )
    client = Client(titles)
    now = datetime(2026, 9, 28, 22, tzinfo=UTC)
    snapshot = fetch_source_catalogue(
        client, source, now=now, cutoff=now - timedelta(days=2), collect_evidence=True
    )
    assert client.calls == 1
    return snapshot


def test_runtime_is_source_agnostic_and_latest_uses_proven_time_not_api_order():
    snapshot = catalogue(
        [
            "Die Nachrichten vom 28.09.2026, 19:00 Uhr",
            "Die Nachrichten vom 28.09.2026, 21:00 Uhr",
            "Die Nachrichten vom 28.09.2026, 20:00 Uhr",
        ]
    )
    assert snapshot.api_latest_id == str(1).zfill(22)
    ordered = sorted(snapshot.episodes, key=episode_order_key, reverse=True)
    assert ordered[0].spotify_episode_id == snapshot.api_latest_id
    assert all(row.timestamp_source is TimestampSource.TITLE for row in snapshot.evidence)


def test_fallback_preserves_episode_and_blocks_unproven_latest():
    snapshot = catalogue(["Die Nachrichten vom 28.09.2026, 21:00 Uhr", "Headline"])
    assert len(snapshot.episodes) == 2
    assert snapshot.api_latest_id is None
    assert snapshot.parser_fallback_count == 1
    assert snapshot.episodes[1].edition_at is None
    assert snapshot.episodes[1].timestamp_source is TimestampSource.FIRST_SEEN
    assert snapshot.evidence[1].parser_outcome == "fallback"


@pytest.mark.parametrize("suffix", ["18:00:30", "100:00", "18:999", "28/09", "202609280"])
def test_clock_prefix_cannot_hide_unrecognized_numeric_candidates(suffix):
    assert parse(f"21:00 Fakty {suffix}", context(zone="Europe/Warsaw")).parsed is None


@pytest.mark.parametrize(
    "title,zone",
    [
        ("CNN 5 cosas 09/28/26 10pm", "America/New_York"),
        ("Die Nachrichten vom 28.09.2026, 21:00 Uhr", "Europe/Berlin"),
    ],
)
def test_latest_proof_respects_local_coarse_day(title, zone):
    snapshot = catalogue([title, "Unproven headline"], zone)
    assert snapshot.episodes[0].edition_at is not None
    assert snapshot.episodes[1].edition_at is None
    assert snapshot.api_latest_id is None


@pytest.mark.parametrize(
    "date_text,day,zone",
    [
        ("29/03/2026", "2026-03-29", "Europe/Berlin"),
        ("25/10/2026", "2026-10-25", "Europe/Warsaw"),
    ],
)
def test_learned_structure_cannot_bypass_dst(date_text, day, zone):
    assert parse(f"Bulletin {date_text} - 02:30", context(day, zone), samples()).parsed is None


def test_inference_preserves_date_order_with_coarse_precision():
    attempt = parse("Bulletin 09/10/2026 - 17:30", context("2026", precision="year"), samples())
    assert attempt.parsed.edition_at.date().isoformat() == "2026-10-09"
    assert (
        parse(
            "Bulletin 09/10/2026 - 17:30", context("2026-09", precision="month"), samples()
        ).parsed
        is None
    )


@pytest.mark.parametrize("year,expected", [("00", 2000), ("68", 2068), ("69", 2069), ("99", 2099)])
def test_universal_keeps_reviewed_short_year_policy(year, expected):
    title = f"NOTICIAS RNE - 28.09.{year} - 17.30H"
    assert parse(title, context(f"{expected}-09-28")).parsed.edition_at.year == expected


def test_repetition_does_not_authorize_unreviewed_clock_only_headlines():
    corpus = tuple(
        TemporalSample(f"{hour}:30 Meeting", context(f"2026-09-{day}"))
        for day, hour in [(26, 17), (26, 18), (27, 17), (27, 19), (28, 17)]
    )
    assert all(parse(row.title, row.context, corpus).parsed is None for row in corpus)


def test_malformed_brackets_cannot_train_a_structure():
    corpus = tuple(
        TemporalSample(row.title.replace(" - ", " - ("), row.context) for row in samples()
    )
    assert all(parse(row.title, row.context, corpus).parsed is None for row in corpus)


def test_fallback_keeps_existing_coarse_retention_while_exact_time_is_precise():
    from news_bulletin_playlist.spotify.first_desired import _retained

    snapshot = catalogue(["Die Nachrichten vom 28.09.2026, 21:00 Uhr", "Headline"])
    now = datetime(2026, 9, 28, 22, tzinfo=UTC)
    cutoff = datetime(2026, 9, 28, 20, tzinfo=UTC)
    assert not _retained(snapshot.episodes[0], cutoff=cutoff, now=now)
    assert _retained(snapshot.episodes[1], cutoff=cutoff, now=now)


@pytest.mark.parametrize(
    "release,precision",
    [
        ("2026-W40-1", "day"),
        ("2026-+9", "month"),
        ("2026- 9", "month"),
        ("+026", "year"),
    ],
)
def test_noncanonical_catalogue_period_never_proves_time(release, precision):
    ctx = context(release, precision=precision)
    assert parse("21:00 Fakty", ctx).parsed is None
    assert parse("Die Nachrichten vom 28.09.2026, 21:00 Uhr", ctx).parsed is None


@pytest.mark.parametrize(
    "day,zone", [("9999-12-31", "America/New_York"), ("0001-01-01", "Europe/Berlin")]
)
def test_extreme_coarse_day_cannot_crash_latest_proof(day, zone):
    from news_bulletin_playlist.spotify.first_catalogue import _provable_latest_id

    snapshot = catalogue(["Headline", "Another headline"])
    extreme = replace(snapshot.episodes[0], release_date_raw=day)
    assert _provable_latest_id([extreme, snapshot.episodes[1]], ZoneInfo(zone)) is None
