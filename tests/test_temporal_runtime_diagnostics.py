"""Refresh snapshot aggregates survive the normal diagnostics event contract."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import SourceCycleOutcome, TemporalRefreshMetrics
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.models import TemporalStrategy
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.public_detector import PublicReleaseDateShape, spotify_show_id
from news_bulletin_playlist.spotify.public_release import PublicReleaseEvidence

NOW = datetime(2026, 9, 28, 19, 31, tzinfo=UTC)
DLF = [f"Die Nachrichten vom 28.09.2026, {h}:00 Uhr" for h in range(10, 20)]


def snapshot(titles, strategy, public):
    source = replace(
        BUILTIN_CATALOG.sources[0], temporal_strategy=strategy, timezone=ZoneInfo("Europe/Berlin")
    )
    items = [
        dict(
            id=f"{i:022}",
            uri=f"spotify:episode:{i:022}",
            name=title,
            release_date="2026-09-28",
            release_date_precision="day",
        )
        for i, title in enumerate(titles)
    ]

    class Client:
        calls = 0

        def show_episodes(self, show_id, *, limit=50, offset=0):
            self.calls += 1
            return catalogue_page(items)

    evidence = (
        None
        if public is None
        else PublicReleaseEvidence(
            source.id,
            spotify_show_id(source),
            f"{public[0]:022}",
            public[1],
            PublicReleaseDateShape.OBJECT_ISO_STRING,
            NOW,
        )
    )
    client = Client()
    result = fetch_source_catalogue(
        client, source, now=NOW, cutoff=NOW - timedelta(hours=48), public_release_evidence=evidence
    )
    assert client.calls == 1
    assert result.evidence == ()  # Runtime aggregates never require per-episode audit data.
    return result


@pytest.mark.parametrize(
    "titles,strategy,public,expected",
    [
        (DLF, TemporalStrategy.AUTO, None, (10, 0, 0, 0, 0, 0)),
        (["Unstructured"], TemporalStrategy.AUTO, (0, "2026-09-28T05:21:00Z"), (0, 1, 0, 1, 0, 0)),
        (["Unstructured"], TemporalStrategy.AUTO, (0, "2026-09-28T21:13:00Z"), (0, 0, 1, 0, 1, 0)),
        (
            ["Las noticias de la SER, 20:43 (28/09/2026)"],
            TemporalStrategy.FULL_DATETIME,
            (0, "2026-09-28T18:43:00Z"),
            (1, 0, 0, 0, 0, 1),
        ),
        (["Unstructured"], TemporalStrategy.AUTO, None, (0, 0, 1, 0, 0, 0)),
        (
            ["Las noticias de la SER, 20:43 (28/09/2026)", "Public", "Coarse"],
            TemporalStrategy.FULL_DATETIME,
            (1, "2026-09-28T05:21:00Z"),
            (1, 1, 1, 1, 0, 0),
        ),
        (
            ["Unstructured"],
            TemporalStrategy.DISABLED,
            (0, "2026-09-28T05:21:00Z"),
            (0, 1, 0, 1, 0, 0),
        ),
    ],
)
def test_snapshot_aggregates_reach_runtime_diagnostics(
    tmp_path, titles, strategy, public, expected
):
    result = snapshot(titles, strategy, public)
    metrics = TemporalRefreshMetrics.from_snapshot(result)
    store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite3")
    store.initialize()
    runner = InstrumentedEngineCycleRunner(None, OperationalDiagnostics(store))
    outcome = SourceCycleOutcome(
        BUILTIN_CATALOG.sources[0].id,
        True,
        len(result.episodes),
        True,
        "change",
        1,
        NOW,
        NOW,
        True,
        True,
        temporal_metrics=metrics,
    )
    runner._emit_source(outcome, NOW, "cycle-test")  # noqa: SLF001
    details = store.list_events(limit=1)[0].details
    assert details["temporal_strategy"] == strategy.value
    assert details["temporal_supporting_rows"] == (10 if titles == DLF else 0)
    assert details["temporal_conflicts"] == 0
    assert (
        tuple(
            details[k]
            for k in (
                "timestamp_title_count",
                "timestamp_public_count",
                "timestamp_first_seen_count",
                "public_release_accepted_count",
                "public_release_rejected_future_count",
                "public_release_shadowed_count",
            )
        )
        == expected
    )
    assert (
        sum(
            details[k]
            for k in (
                "timestamp_title_count",
                "timestamp_public_count",
                "timestamp_first_seen_count",
            )
        )
        == details["episode_count"]
    )
    assert not {"title", "release_date_raw", "layout"} & details.keys()


@pytest.mark.parametrize(
    "refreshed,ok,false_negative,event",
    [
        (False, True, False, "source_cycle_completed"),
        (False, False, False, "source_state_preserved"),
        (True, False, False, "source_state_preserved"),
        (True, True, True, "source_detector_false_negative"),
    ],
)
def test_event_variants_only_emit_available_current_refresh_metrics(
    tmp_path, refreshed, ok, false_negative, event
):
    metrics = TemporalRefreshMetrics.from_snapshot(snapshot(DLF, TemporalStrategy.AUTO, None))
    store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite3")
    store.initialize()
    runner = InstrumentedEngineCycleRunner(None, OperationalDiagnostics(store))
    outcome = SourceCycleOutcome(
        BUILTIN_CATALOG.sources[0].id,
        ok,
        10,
        refreshed,
        "change",
        1,
        NOW,
        NOW,
        True,
        False,
        detector_false_negative=false_negative,
        temporal_metrics=metrics,
    )
    runner._emit_source(outcome, NOW, "cycle-test")  # noqa: SLF001
    row = store.list_events(limit=1)[0]
    assert row.event_name == event
    assert ("temporal_strategy" in row.details) is refreshed
    # Actual failed/unchanged engine outcomes leave the optional metric block absent.
    runner._emit_source(replace(outcome, temporal_metrics=None), NOW, "cycle-empty")  # noqa: SLF001
    assert "temporal_strategy" not in store.list_events(limit=1)[0].details


@pytest.mark.parametrize(
    "public,field",
    [
        ((0, "bad"), "public_release_invalid_count"),
        ((0, "2026-09-27T05:21:00Z"), "public_release_catalogue_conflict_count"),
        ((1, "2026-09-28T05:21:00Z"), "public_release_identity_mismatch_count"),
    ],
)
def test_other_public_outcomes_are_aggregated(public, field):
    metrics = TemporalRefreshMetrics.from_snapshot(
        snapshot(["Unstructured"], TemporalStrategy.AUTO, public)
    )
    assert getattr(metrics, field) == 1
    assert metrics.timestamp_first_seen_count == 1
    assert metrics.public_release_accepted_count == metrics.timestamp_public_count == 0
