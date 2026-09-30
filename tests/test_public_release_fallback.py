"""Literal public release fallback bound to authorized catalogue identity."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import EngineConfig, SourceId, TemporalStrategy
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.first_desired import _retained
from news_bulletin_playlist.spotify.first_models import (
    RefreshReason,
    TimestampSource,
    episode_order_key,
)
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetectorCycleSummary,
    PublicDetectorJournal,
    PublicEpisodeObservation,
    spotify_show_id,
)
from news_bulletin_playlist.spotify.public_detector import PublicReleaseDateShape as Shape
from news_bulletin_playlist.spotify.public_release import (
    PUBLIC_RELEASE_CLOCK_SKEW,
    PublicReleaseEvidence,
)
from news_bulletin_playlist.spotify.public_release import (
    PublicReleaseOutcome as Outcome,
)

NOW = datetime(2026, 9, 28, 5, 37, 47, tzinfo=UTC)
EPISODE = "6kVcmyoWqymilhqVzMOJ0E"
OLD = "7KDb9bGN2MIdmkFaiSOnAc"
SOURCE = replace(
    BUILTIN_CATALOG.sources[0],
    temporal_strategy=TemporalStrategy.AUTO,
    timezone=ZoneInfo("Europe/Madrid"),
)


def evidence(raw="2026-09-28T05:21:00Z", *, episode=EPISODE, observed=NOW):
    return PublicReleaseEvidence(
        SOURCE.id, spotify_show_id(SOURCE), episode, raw, Shape.OBJECT_ISO_STRING, observed
    )


def fetch(
    public=None,
    *,
    title="Iberdrola, Riquelme, el BCE, Trump y Anthropic, Nvidia y guerra híbrida",
    source=SOURCE,
    day="2026-09-28",
    precision="day",
    episode=EPISODE,
    extra=(),
):
    items = [
        dict(
            id=episode,
            uri=f"spotify:episode:{episode}",
            name=title,
            release_date=day,
            release_date_precision=precision,
            duration_ms=100000,
        )
    ]
    items.extend(extra)

    class Client:
        calls = 0

        def show_episodes(self, show_id, *, limit=50, offset=0):
            self.calls += 1
            return catalogue_page(items)

    client = Client()
    snapshot = fetch_source_catalogue(
        client,
        source,
        now=NOW,
        cutoff=NOW - timedelta(hours=48),
        public_release_evidence=public,
        collect_evidence=True,
    )
    assert client.calls == snapshot.request_count == 1
    assert len(snapshot.episodes) == len(items)
    return snapshot


@pytest.mark.parametrize(
    "raw,observed,day,episode",
    [
        ("2026-09-28T05:21:00Z", NOW, "2026-09-28", EPISODE),
        ("2026-09-25T05:27:00Z", datetime(2026, 9, 27, 18, 58, 17, tzinfo=UTC), "2026-09-25", OLD),
        (
            "2026-09-27T13:58:00Z",
            datetime(2026, 9, 27, 14, 2, 48, tzinfo=UTC),
            "2026-09-27",
            EPISODE,
        ),
        (
            "2026-09-28T00:45:00Z",
            datetime(2026, 9, 28, 1, 4, 47, tzinfo=UTC),
            "2026-09-28",
            EPISODE,
        ),
    ],
)
def test_real_expansion_bootstrap_reuters_abc(raw, observed, day, episode):
    result = fetch(evidence(raw, observed=observed, episode=episode), day=day, episode=episode)
    item = result.episodes[0]
    assert item.edition_at == datetime.fromisoformat(raw)
    assert item.timestamp_source is TimestampSource.SPOTIFY_PUBLIC_RELEASE
    assert result.evidence[0].parser_outcome == "fallback"
    assert result.evidence[0].public_release_outcome is Outcome.ACCEPTED
    if episode == OLD:
        assert not _retained(item, now=observed, cutoff=observed - timedelta(hours=48))


@pytest.mark.parametrize(
    "changes,outcome",
    [
        ({"source_id": SourceId("other")}, Outcome.IDENTITY_MISMATCH),
        ({"spotify_show_id": "x" * 22}, Outcome.IDENTITY_MISMATCH),
        ({"spotify_episode_id": OLD}, Outcome.IDENTITY_MISMATCH),
        ({"release_date_raw": "bad"}, Outcome.INVALID),
        ({"release_date_raw": "2026-09-28T05:21:00+00:99"}, Outcome.INVALID),
        ({"release_date_raw": "2026-09-28T05:21:00"}, Outcome.INVALID),
        ({"release_date_raw": "2026-09-28"}, Outcome.INVALID),
        ({"release_date_raw": "２０２６-09-28T05:21:00Z"}, Outcome.INVALID),
        ({"release_date_raw": "2026-09-28X05:21:00Z"}, Outcome.INVALID),
        ({"release_date_raw": "9999-12-31T23:59:59-23:00"}, Outcome.INVALID),
        ({"release_date_raw": "2026-09-29T05:21:00Z"}, Outcome.CATALOGUE_CONFLICT),
        ({"release_date_raw": "2026-09-28T05:42:48Z"}, Outcome.FUTURE),
        (
            {
                "release_date_raw": "2026-09-28T21:13:00Z",
                "observed_at": datetime(2026, 9, 28, 19, 31, tzinfo=UTC),
            },
            Outcome.FUTURE,
        ),
        ({"observed_at": datetime(2026, 9, 28, 5, 37)}, Outcome.INVALID),
    ],
)
def test_rejected_evidence_preserves_episode(changes, outcome):
    result = fetch(replace(evidence(), **changes))
    assert result.episodes[0].timestamp_source is TimestampSource.FIRST_SEEN
    assert result.episodes[0].edition_at is None
    assert result.evidence[0].public_release_outcome is outcome


@pytest.mark.parametrize(
    "shape", [shape for shape in Shape if shape is not Shape.OBJECT_ISO_STRING]
)
def test_unsupported_shapes_fail_closed(shape):
    assert (
        fetch(replace(evidence(), release_date_shape=shape)).evidence[0].public_release_outcome
        is Outcome.INVALID
    )


@pytest.mark.parametrize("seconds,accepted", [(299, True), (300, True), (301, False)])
def test_explicit_five_minute_skew(seconds, accepted):
    assert timedelta(minutes=5) == PUBLIC_RELEASE_CLOCK_SKEW
    raw = (NOW + timedelta(seconds=seconds)).isoformat()
    item = fetch(evidence(raw)).episodes[0]
    assert (item.edition_at is not None) is accepted


@pytest.mark.parametrize(
    "day,precision,raw,accepted",
    [
        ("2026-09", "month", "2026-09-02T12:00:00Z", True),
        ("2026", "year", "2026-01-01T12:00:00Z", True),
        ("2026-08", "month", "2026-09-02T12:00:00Z", False),
        ("2025", "year", "2026-01-01T12:00:00Z", False),
        ("2026-09-28", "day", "2026-09-27T23:00:00Z", True),
        ("2026-09-27", "day", "2026-09-27T23:00:00Z", False),
    ],
)
def test_catalogue_period_uses_source_local_calendar(day, precision, raw, accepted):
    assert (
        fetch(evidence(raw), day=day, precision=precision).episodes[0].edition_at is not None
    ) is accepted


def test_disabled_skips_semantics_but_keeps_public_fallback():
    source = replace(SOURCE, temporal_strategy=TemporalStrategy.DISABLED)
    assert (
        fetch(evidence(), source=source).episodes[0].timestamp_source
        is TimestampSource.SPOTIFY_PUBLIC_RELEASE
    )
    assert fetch(source=source).episodes[0].timestamp_source is TimestampSource.FIRST_SEEN


@pytest.mark.parametrize("raw", ["2026-09-28T07:21:00+02:00", "2026-09-28T01:21:00-04:00"])
def test_public_offsets_are_literal_instants(raw):
    item = fetch(evidence(raw)).episodes[0]
    assert item.edition_at == datetime(2026, 9, 28, 5, 21, tzinfo=UTC)
    assert item.timestamp_source is TimestampSource.SPOTIFY_PUBLIC_RELEASE


@pytest.mark.parametrize(
    "title,strategy,public,expected,zone",
    [
        (
            "Las noticias de la SER, 20:43 (28/09/2026)",
            TemporalStrategy.FULL_DATETIME,
            "2026-09-28T18:43:00Z",
            "2026-09-28T18:43:00Z",
            "Europe/Madrid",
        ),
        (
            "Las noticias de Onda Cero de las 23:00h (28/09/2026)",
            TemporalStrategy.FULL_DATETIME,
            "2026-09-28T21:02:00Z",
            "2026-09-28T21:00:00Z",
            "Europe/Madrid",
        ),
        (
            "16:00 Fakty",
            TemporalStrategy.CLOCK_CATALOGUE_DAY,
            "2026-09-28T14:00:00Z",
            "2026-09-28T14:00:00Z",
            "Europe/Warsaw",
        ),
        (
            "NOTICIAS RNE - 28.09.2026 - 21.00H",
            TemporalStrategy.FULL_DATETIME,
            "2026-09-28T21:13:00Z",
            "2026-09-28T19:00:00Z",
            "Europe/Madrid",
        ),
    ],
)
def test_semantic_title_wins_even_if_public_is_different_or_impossible(
    title, strategy, public, expected, zone
):
    source = replace(SOURCE, temporal_strategy=strategy, timezone=ZoneInfo(zone))
    result = fetch(
        evidence(public, observed=datetime(2026, 9, 28, 19, 31, tzinfo=UTC)),
        source=source,
        title=title,
    )
    assert result.episodes[0].timestamp_source is TimestampSource.TITLE
    assert result.episodes[0].edition_at == datetime.fromisoformat(expected)
    assert result.evidence[0].public_release_outcome is Outcome.SHADOWED


def test_only_matching_id_receives_public_with_no_title_matching():
    result = fetch(
        evidence(),
        title="Entirely different catalogue title",
        extra=[
            dict(
                id=OLD,
                uri=f"spotify:episode:{OLD}",
                name="Other episode",
                release_date="2026-09-28",
                release_date_precision="day",
            )
        ],
    )
    assert [e.timestamp_source for e in result.episodes] == [
        TimestampSource.SPOTIFY_PUBLIC_RELEASE,
        TimestampSource.FIRST_SEEN,
    ]
    assert result.api_latest_id is None  # Same-day coarse sibling prevents proof.


def test_restart_and_later_catalogue_preserve_validated_public(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    first = fetch(evidence()).episodes
    journal.replace_source_episodes(SOURCE.id, first, coverage_proven=True)
    journal = SpotifyFirstJournal(store)
    assert journal.list_episodes() == first
    journal.replace_source_episodes(SOURCE.id, fetch().episodes, coverage_proven=True)
    assert journal.list_episodes() == first
    assert (
        journal.list_episode_history(
            observed_since=NOW - timedelta(days=1), observed_until=NOW + timedelta(days=1)
        )
        == first
    )
    # A newly demonstrated title replaces lower-priority publication evidence.
    semantic = fetch(
        title="Las noticias de la SER, 07:00 (28/09/2026)",
        source=replace(SOURCE, temporal_strategy=TemporalStrategy.FULL_DATETIME),
    ).episodes
    journal.replace_source_episodes(SOURCE.id, semantic, coverage_proven=True)
    assert journal.list_episodes()[0].timestamp_source is TimestampSource.TITLE


def test_mixed_ordering_and_cutoff_are_deterministic():
    public = fetch(evidence()).episodes[0]
    title = replace(
        public,
        spotify_episode_id=OLD,
        spotify_episode_uri=f"spotify:episode:{OLD}",
        edition_at=public.edition_at - timedelta(minutes=1),
        timestamp_source=TimestampSource.TITLE,
    )
    fallback = replace(
        public,
        spotify_episode_id="x" * 22,
        spotify_episode_uri="spotify:episode:" + "x" * 22,
        edition_at=None,
        timestamp_source=TimestampSource.FIRST_SEEN,
    )
    assert sorted([title, public, fallback], key=episode_order_key) == sorted(
        [fallback, public, title], key=episode_order_key
    )
    cutoff = public.edition_at
    assert not _retained(public, now=NOW, cutoff=cutoff)  # Existing cutoff is exclusive.
    assert _retained(public, now=NOW, cutoff=cutoff - timedelta(microseconds=1))
    assert not _retained(title, now=NOW, cutoff=cutoff)
    assert _retained(fallback, now=NOW, cutoff=cutoff)


def test_real_previous_sqlite_schema_migrates_without_loss(tmp_path):
    # Load the actual preceding revision's schema, not a weakened mock schema.
    store = SQLiteStore(tmp_path / "previous.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    title = fetch(
        title="Las noticias de la SER, 07:00 (28/09/2026)",
        source=replace(SOURCE, temporal_strategy=TemporalStrategy.FULL_DATETIME),
    ).episodes[0]
    fallback = fetch(episode=OLD).episodes[0]
    journal.replace_source_episodes(SOURCE.id, [title, fallback], coverage_proven=True)
    journal.record_refresh(
        SOURCE,
        refreshed_at=NOW,
        reason=RefreshReason.CHANGE,
        detector_episode_id=EPISODE,
        api_episode_id=EPISODE,
        request_count=1,
        complete=True,
        coverage_proven=True,
        coverage_cutoff_at=NOW - timedelta(hours=48),
    )
    with sqlite3.connect(store.path) as db:
        fixture = Path(__file__).parent / "fixtures/temporal/previous_episode_schema.sql"
        schemas = fixture.read_text().split(";", 2)[:2]
        for table, sql in zip(
            ("spotify_first_episode_state", "spotify_first_episode_history"), schemas, strict=True
        ):
            db.execute(f"ALTER TABLE {table} RENAME TO {table}_saved")
            db.execute(sql)
            db.execute(f"INSERT INTO {table} SELECT * FROM {table}_saved")
            db.execute(f"DROP TABLE {table}_saved")
        db.execute(
            "CREATE INDEX idx_spotify_first_history_source_seen "
            "ON spotify_first_episode_history(source_id,last_seen_at DESC,spotify_episode_id)"
        )
        db.execute(
            "CREATE INDEX idx_spotify_first_episode_source_seen "
            "ON spotify_first_episode_state(source_id,last_seen_at DESC)"
        )
        db.execute("UPDATE database_format SET format_version=2")
        before = {
            t: db.execute(f"SELECT * FROM {t}").fetchall()
            for t in (
                "spotify_first_episode_state",
                "spotify_first_episode_history",
                "spotify_first_source_state",
            )
        }
    store.initialize()
    assert store.format_version() == 3
    store.initialize()
    with sqlite3.connect(store.path) as db:
        assert before == {t: db.execute(f"SELECT * FROM {t}").fetchall() for t in before}
        assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")} >= {
            "idx_spotify_first_history_source_seen",
            "idx_spotify_first_episode_source_seen",
        }
    journal.replace_source_episodes(SOURCE.id, fetch(evidence()).episodes, coverage_proven=False)
    assert (
        SpotifyFirstJournal(store).list_episodes()[0].timestamp_source
        is TimestampSource.SPOTIFY_PUBLIC_RELEASE
    )


def test_incompatible_migration_rolls_back(tmp_path):
    store = SQLiteStore(tmp_path / "broken.sqlite3")
    store.initialize()
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE spotify_first_episode_history")
        db.execute("CREATE TABLE spotify_first_episode_history (bad TEXT)")
        db.execute("INSERT INTO spotify_first_episode_history VALUES ('preserved')")
        db.execute("UPDATE database_format SET format_version=2")
    with pytest.raises(PersistenceError, match="incompatible"):
        store.initialize()
    assert store.format_version() == 2
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT * FROM spotify_first_episode_history").fetchall() == [
            ("preserved",)
        ]


@pytest.mark.parametrize("degraded", [False, True])
@pytest.mark.parametrize("reason", [RefreshReason.BOOTSTRAP, RefreshReason.CHANGE])
def test_engine_refresh_passes_existing_public_evidence_without_requests(
    tmp_path, degraded, reason
):
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    detector = PublicDetectorJournal(store)
    detector.initialize()
    observation = PublicEpisodeObservation(
        SOURCE.id,
        spotify_show_id(SOURCE),
        EPISODE,
        f"spotify:episode:{EPISODE}",
        "Public title",
        "Public title",
        "2026-09-28T05:21:00Z",
        None,
        None,
        NOW,
        Shape.OBJECT_ISO_STRING,
    )
    detector.record_success(observation)
    if degraded:
        # The persisted last good observation remains but is not promoted anew.
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE spotify_public_detector_state SET consecutive_detector_failures=1")
    runner = object.__new__(EngineRunner)
    runner.config = EngineConfig(sources=(SOURCE,), playlists=())
    runner.first_journal = journal
    runner.detector_journal = detector
    runner.max_catalogue_pages = 4

    class Client:
        calls = 0

        def show_episodes(self, show_id, *, limit=50, offset=0):
            self.calls += 1
            return catalogue_page(
                [
                    dict(
                        id=EPISODE,
                        uri=f"spotify:episode:{EPISODE}",
                        name="Different authorized title",
                        release_date="2026-09-28",
                        release_date_precision="day",
                    )
                ]
            )

    client = Client()
    summary = PublicDetectorCycleSummary(1, 1, 0, 0, changed_source_ids=(SOURCE.id,))
    outcome = runner._refresh_source(SOURCE, reason, summary, client, now=NOW)  # noqa: SLF001
    assert outcome.ok and outcome.refreshed
    assert client.calls == outcome.api_request_count == 1
    expected = TimestampSource.FIRST_SEEN if degraded else TimestampSource.SPOTIFY_PUBLIC_RELEASE
    assert journal.list_episodes()[0].timestamp_source is expected
    assert outcome.temporal_metrics is not None
    assert outcome.temporal_metrics.timestamp_public_count == int(not degraded)
    assert outcome.temporal_metrics.timestamp_first_seen_count == int(degraded)


@pytest.mark.parametrize(
    "source,edition",
    [
        (TimestampSource.TITLE, None),
        (TimestampSource.SPOTIFY_PUBLIC_RELEASE, None),
        (TimestampSource.FIRST_SEEN, NOW),
    ],
)
def test_invalid_exact_provenance_combinations_are_rejected(source, edition):
    with pytest.raises(ValueError, match="provenance"):
        replace(fetch().episodes[0], timestamp_source=source, edition_at=edition)
