"""Source-local AUTO and explicit reusable capabilities, through catalogue ingestion."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.models import ParserId
from news_bulletin_playlist.models import TemporalStrategy as Strategy
from news_bulletin_playlist.providers.base import CatalogueParseContext
from news_bulletin_playlist.providers.temporal_detector import TemporalSample
from news_bulletin_playlist.registry import get_temporal_detector
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.first_desired import _retained
from news_bulletin_playlist.spotify.first_models import TimestampSource, episode_order_key

NOW = datetime(2026, 9, 30, 18, tzinfo=UTC)
DLF = [
    f"Die Nachrichten vom 28.09.2026, {clock} Uhr"
    for clock in (
        "21:00",
        "20:00",
        "19:00",
        "18:00",
        "17:30",
        "17:00",
        "16:30",
        "15:59",
        "15:30",
        "15:00",
    )
]
FOREIGN = [
    "CNN 5 cosas 09/28/26 6am",
    "Las noticias de la SER, 20:43 (28/09/2026)",
    "NOTICIAS RNE - 28092026 - 1130H",
    "16:00 Fakty",
    "09:00H | 28 SEP 2026 | BOLETÍN",
]


def fetch(titles, *, strategy=Strategy.AUTO, zone="Europe/Berlin", days=None, precision="day"):
    source = replace(
        BUILTIN_CATALOG.sources[0],
        parser_id=ParserId("release_date_title"),
        temporal_strategy=strategy,
        timezone=ZoneInfo(zone),
    )
    items = [
        dict(
            id=str(i).zfill(22),
            uri=f"spotify:episode:{i:022}",
            name=title,
            release_date=days[i] if days else "2026-09-28",
            release_date_precision=precision,
        )
        for i, title in enumerate(titles)
    ]

    class Client:
        calls = 0

        def show_episodes(self, show_id, *, limit=50, offset=0):
            self.calls += 1
            assert offset == 0
            return catalogue_page(items)

    client = Client()
    snapshot = fetch_source_catalogue(
        client, source, now=NOW, cutoff=NOW - timedelta(hours=48), collect_evidence=True
    )
    assert client.calls == 1
    assert len(snapshot.episodes) == len(titles)
    assert snapshot.filter_rejected_count == 0
    return snapshot


def test_dlf_auto_learns_same_day_without_a_reviewed_phrase():
    result = fetch(DLF)
    assert result.temporal_supporting_rows == 10
    assert result.temporal_conflicts == 0
    assert result.episodes[0].edition_at == datetime(2026, 9, 28, 19, tzinfo=UTC)
    assert all(row.temporal_evidence == "catalogue_structure" for row in result.evidence)
    assert result.api_latest_id == "0" * 22
    # Changing the phrase has no effect: no language or broadcaster allowlist exists.
    renamed = fetch([title.replace("Die Nachrichten vom", "Zyxw Bulletin |") for title in DLF])
    assert [e.edition_at for e in renamed.episodes] == [e.edition_at for e in result.episodes]


@pytest.mark.parametrize(
    "pattern,zone",
    [
        ("Las noticias de la SER, {h}:43 (28/09/2026)", "Europe/Madrid"),
        ("NOTICIAS RNE - 28092026 - {h}30H", "Europe/Madrid"),
        ("Las noticias de Onda Cero de las {h}:30h (28/09/2026)", "Europe/Madrid"),
        ("CNN 5 cosas 09/28/26 {h}am", "America/New_York"),
        ("{h}:30H | 28 SEP 2026 | BOLETÍN", "Europe/Madrid"),
        ("Journal 28/09/2026 {h}h30 GMT", "UTC"),
    ],
)
def test_historical_full_datetime_families_can_use_auto_without_parser_ids(pattern, zone):
    result = fetch([pattern.format(h=h) for h in range(7, 12)], zone=zone)
    assert result.temporal_supporting_rows == 5
    assert result.parser_fallback_count == 0
    assert all(e.temporal_evidence == "catalogue_structure" for e in result.evidence)


@pytest.mark.parametrize("title", FOREIGN)
@pytest.mark.parametrize("noise", [0, 1, 4])
def test_single_foreign_contract_never_leaks_into_auto(title, noise):
    result = fetch([title, *[f"Unrelated headline {i}" for i in range(noise)]])
    assert result.parser_fallback_count == noise + 1
    assert result.temporal_supporting_rows == 0


@pytest.mark.parametrize(
    "strategy,title",
    [
        (Strategy.CLOCK_CATALOGUE_DAY, FOREIGN[1]),
        (Strategy.FULL_DATETIME, "16:00 Fakty"),
        (Strategy.PARTIAL_DATE_CATALOGUE_DAY, "16:00 Fakty"),
        (Strategy.EXPLICIT_UTC, FOREIGN[1]),
    ],
)
def test_override_cannot_consume_incompatible_evidence(strategy, title):
    assert fetch([title], strategy=strategy).parser_fallback_count == 1


def test_disabled_never_uses_even_proven_structure():
    result = fetch(DLF, strategy=Strategy.DISABLED)
    assert result.parser_fallback_count == 10
    assert all(e.temporal_evidence == "disabled" for e in result.evidence)


@pytest.mark.parametrize(
    "family,zone,expected",
    [
        (
            [
                "Boletín diario | 29-09-2026 | 14:30",
                "Boletín diario | 29-09-2026 | 13:30",
                "Boletín diario | 29-09-2026 | 12:30",
                "Boletín diario | 28-09-2026 | 23:30",
                "Boletín diario | 28-09-2026 | 22:30",
            ],
            "Europe/Madrid",
            "2026-09-29T12:30:00+00:00",
        ),
        (
            [
                "Morning Update — 09/29/26 — 6am",
                "Morning Update — 09/29/26 — 7am",
                "Morning Update — 09/29/26 — 8am",
                "Morning Update — 09/28/26 — 9am",
                "Morning Update — 09/28/26 — 10am",
            ],
            "America/New_York",
            "2026-09-29T10:00:00+00:00",
        ),
    ],
)
def test_unseen_families_infer_order_clock_and_normalization(family, zone, expected):
    result = fetch(family, zone=zone, days=["2026-09-29"] * 3 + ["2026-09-28"] * 2)
    assert result.parser_fallback_count == 0
    assert result.episodes[0].edition_at.astimezone(UTC).isoformat() == expected
    assert result.temporal_supporting_rows == 5


@pytest.mark.parametrize(
    "clock,utc_hour,minute",
    [
        ("1730", 15, 30),
        ("1630", 14, 30),
        ("1600", 14, 0),
        ("1530", 13, 30),
        ("1500", 13, 0),
        ("1400", 12, 0),
    ],
)
def test_damaged_date_uses_only_official_day(clock, utc_hour, minute):
    title = f"NOTICIAS - RNE 2809206 - {clock}H"
    assert fetch([title]).parser_fallback_count == 1
    override = fetch([title], strategy=Strategy.CLOCK_CATALOGUE_DAY, zone="Europe/Madrid")
    assert override.episodes[0].edition_at == datetime(2026, 9, 28, utc_hour, minute, tzinfo=UTC)
    assert override.evidence[0].parser_outcome == "catalogue_context"
    trained = fetch(
        [*[f"NOTICIAS - RNE 28092026 - {hour}00H" for hour in range(8, 13)], title],
        zone="Europe/Madrid",
    )
    assert trained.episodes[-1].edition_at == override.episodes[0].edition_at
    assert trained.evidence[-1].temporal_evidence == "catalogue_structure"


@pytest.mark.parametrize("clock,utc", [("16:00", "14:00"), ("15:30", "13:30"), ("6:30", "04:30")])
def test_clock_capability_is_reusable_without_a_product_name(clock, utc):
    for label in ("Fakty", "Fakty sportowe", "Invented edition"):
        result = fetch(
            [f"{clock} {label}"],
            strategy=Strategy.CLOCK_CATALOGUE_DAY,
            zone="Europe/Warsaw",
            days=["2026-09-25"],
        )
        assert result.episodes[0].edition_at.astimezone(UTC).strftime("%H:%M") == utc


@pytest.mark.parametrize("hour", [18, 12, 11])
def test_partial_date_reuses_exact_day_and_respects_explicit_gmt(hour):
    title = f"Tranche d'information 28/09 {hour}h00 GMT"
    strategy = Strategy.PARTIAL_DATE_CATALOGUE_DAY
    result = fetch([title], strategy=strategy, zone="UTC")
    assert result.episodes[0].edition_at == datetime(2026, 9, 28, hour, tzinfo=UTC)
    assert (
        fetch([title], strategy=strategy, days=["2026-09-29"], zone="UTC").parser_fallback_count
        == 1
    )
    assert fetch([title], strategy=strategy, zone="Europe/Madrid").parser_fallback_count == 1
    assert fetch([title]).parser_fallback_count == 1


@pytest.mark.parametrize(
    "day,precision", [("2026-09-28", "day"), ("2026-09", "month"), ("2026", "year")]
)
def test_partial_date_capability_does_not_discard_a_complete_calendar(day, precision):
    result = fetch(
        ["Journal en français facile 28/09/2026 16h00 GMT"],
        strategy=Strategy.PARTIAL_DATE_CATALOGUE_DAY,
        zone="UTC",
        days=[day],
        precision=precision,
    )
    assert result.episodes[0].edition_at == datetime(2026, 9, 28, 16, tzinfo=UTC)
    assert result.evidence[0].parser_outcome == "exact"


@pytest.mark.parametrize(
    "zone,day,clock",
    [
        *[
            (zone, day, "02:30")
            for zone in ("Europe/Madrid", "Europe/Berlin", "Europe/Warsaw")
            for day in ("2026-03-29", "2026-10-25")
        ],
        ("America/New_York", "2026-03-08", "02:30"),
        ("America/New_York", "2026-11-01", "01:30"),
    ],
)
def test_dst_every_capability_fails_closed(zone, day, clock):
    ctx = CatalogueParseContext(day, "day", ZoneInfo(zone))
    stamp = datetime.fromisoformat(day).strftime("%d/%m/%Y")
    for strategy, title in [
        (Strategy.FULL_DATETIME, f"Edition {stamp} - {clock}"),
        (Strategy.CLOCK_CATALOGUE_DAY, f"{clock} Edition"),
        (Strategy.PARTIAL_DATE_CATALOGUE_DAY, f"Edition {stamp[:5]} {clock}"),
    ]:
        assert get_temporal_detector((), strategy).parse(title, context=ctx).parsed is None


@pytest.mark.parametrize(
    "title",
    [
        "Trump hablará a las 18:00, reunión a las 20:00 y rueda de prensa a las 21:00",
        "Programa 2026 - entrevista a las 21:00",
        "Resultados 20-10, edición 2026",
        "25 años después de aquel 11 de septiembre",
        "Especial 20:30: las claves del debate",
        "UN News Today 28 September 2026",
        "¿Qué pasará con los 177 mil salvadoreños sin TPS?",
        "Reino Unido apunta a Irán",
        "Parques Reunidos, jubilación, Soitec, Geely, Musk, Altman y Xi Jinping",
    ],
)
@pytest.mark.parametrize("strategy", list(Strategy))
def test_adversarial_titles_cannot_be_authorized_by_any_capability(title, strategy):
    assert fetch([title], strategy=strategy).parser_fallback_count == 1


@pytest.mark.parametrize(
    "titles",
    [
        [
            "Cacería en Doral: ICE deja familias separadas",
            "Hackers aseguran que vulneraron al FBI",
            "Huracán Polo impacta en el Pacífico",
            "Estados Unidos y China se disputan el control de la IA",
            "25 años después de aquel 11 de septiembre",
            "¿Qué pasará con los 177 mil salvadoreños sin TPS?",
        ],
        [
            "Reino Unido apunta a Irán",
            "Cumbre de superpotencias en Washington",
            "Alemania crece más de lo previsto",
            "Delcy Rodríguez y Donald Trump se reúnen por primera vez",
            "Se firma el acuerdo sobre Groenlandia",
            "Merz bajo presión tras 'desastre' electoral",
        ],
        ["Parques Reunidos, jubilación, Soitec, Geely, Musk, Altman y Xi Jinping"],
        ["UN News Today 28 September 2026"],
    ],
)
def test_supplied_nplus_dw_expansion_and_un_catalogues_have_zero_exact_times(titles):
    result = fetch(titles)
    assert result.parser_fallback_count == len(titles)
    assert all(e.timestamp_source is TimestampSource.FIRST_SEEN for e in result.episodes)


def test_sparse_repeated_or_changed_formats_do_not_train():
    for titles in (
        DLF[:1],
        DLF[:2],
        [DLF[0]] * 5,
        DLF[:5] + [t.replace("Nachrichten", "Unexpected") for t in DLF[5:]],
    ):
        assert fetch(titles).parser_fallback_count == len(titles)


def test_mixed_auto_exact_fallback_retention_and_latest_are_deterministic():
    result = fetch([*reversed(DLF), "Las noticias de la SER, 20:43 (28/09/2026)"])
    assert result.parser_fallback_count == 1
    assert result.api_latest_id is None
    assert result.episodes[-1].timestamp_source is TimestampSource.FIRST_SEEN
    cutoff = NOW - timedelta(hours=48)
    retained = [e for e in result.episodes if _retained(e, cutoff=cutoff, now=NOW)]
    assert [e.title for e in retained] == [DLF[0], result.episodes[-1].title]
    ordered = sorted(retained, key=episode_order_key, reverse=True)
    assert ordered == sorted(reversed(retained), key=episode_order_key, reverse=True)
    assert len(result.episodes) == 11


@pytest.mark.parametrize("precision,day", [("month", "2026-09"), ("year", "2026")])
def test_catalogue_clock_routes_require_day_precision(precision, day):
    for strategy, title in [
        (Strategy.CLOCK_CATALOGUE_DAY, "16:00 Edition"),
        (Strategy.PARTIAL_DATE_CATALOGUE_DAY, "Edition 28/09 16:00"),
    ]:
        assert (
            fetch([title], strategy=strategy, precision=precision, days=[day]).parser_fallback_count
            == 1
        )


def test_auto_precedes_configured_capability():
    result = fetch(DLF, strategy=Strategy.CLOCK_CATALOGUE_DAY)
    assert result.parser_fallback_count == 0
    assert all(e.temporal_evidence == "catalogue_structure" for e in result.evidence)


def test_override_preserves_proven_date_order_and_scopes_catalogue_conflict_per_row():
    ctx = CatalogueParseContext("2026-09-28", "day", ZoneInfo("Europe/Berlin"))
    corpus = tuple(TemporalSample(t, ctx) for t in DLF)
    detector = get_temporal_detector(corpus, Strategy.FULL_DATETIME)
    # DMY has been demonstrated; MDY cannot be chosen just to fit this new official day.
    changed = DLF[0].replace("28.09.2026", "09.10.2026")
    changed_ctx = replace(ctx, release_date="2026-09-10")
    assert detector.parse(changed, context=changed_ctx).parsed is None

    bad = TemporalSample(DLF[0].replace("28.09.2026", "29.09.2026"), ctx)
    detector = get_temporal_detector((*corpus, bad), Strategy.FULL_DATETIME)
    assert detector.conflicts == 1

    good_attempt = detector.parse(DLF[0], context=ctx)
    assert good_attempt.parsed is not None
    assert good_attempt.temporal_evidence == "configured_capability"

    bad_attempt = detector.parse(bad.title, context=bad.context)
    assert bad_attempt.parsed is None
    assert bad_attempt.temporal_evidence == "catalogue_date_conflict"

    # A sibling contradiction does not permit the configured capability to switch
    # away from the date order established by the compatible catalogue rows.
    assert detector.parse(changed, context=changed_ctx).parsed is None


@pytest.mark.parametrize("suffix", ["GMT", "GMT.", "UTC", "UTC."])
def test_explicit_zero_offset_cannot_be_hidden_by_trailing_punctuation(suffix):
    titles = [f"Edition 28/09/2026 {hour}h30 {suffix}" for hour in range(7, 12)]
    assert (
        fetch(titles, strategy=Strategy.FULL_DATETIME, zone="Europe/Madrid").parser_fallback_count
        == 5
    )
    assert fetch(titles, zone="UTC").parser_fallback_count == 0


@pytest.mark.parametrize("bad", ["２１:00", "٢١:00", "21∶00", "21:00:30", "21:00 22:00"])
def test_bad_or_confusable_clock_cannot_reuse_proven_structure(bad):
    ctx = CatalogueParseContext("2026-09-28", "day", ZoneInfo("Europe/Berlin"))
    detector = get_temporal_detector(tuple(TemporalSample(t, ctx) for t in DLF))
    assert detector.parse(DLF[0].replace("21:00", bad), context=ctx).parsed is None
