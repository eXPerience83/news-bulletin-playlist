from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_bulletin_playlist.models import CanonicalEdition, PlaylistId, SourceId
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore


def _edition(*, title: str) -> CanonicalEdition:
    return CanonicalEdition(
        source_id=SourceId("rne"),
        source_native_id="rne-asset-1",
        title=title,
        published_at=datetime(2026, 8, 30, 9, 5, tzinfo=UTC),
        edition_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
        duration_seconds=300,
    )


def test_stale_replays_do_not_regress_canonical_match_or_latest_states(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "data" / "state.sqlite3")
    store.initialize()
    base = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)

    older = _edition(title="older metadata")
    newer = _edition(title="newer metadata")
    store.upsert_editions((newer,), observed_at=base + timedelta(minutes=20))
    store.upsert_editions((older,), observed_at=base + timedelta(minutes=10))
    assert store.get_edition(SourceId("rne"), "rne-asset-1") == newer

    store.set_match_state(
        SourceId("rne"),
        "rne-asset-1",
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:newer",
        diagnostics="newer match",
        updated_at=base + timedelta(minutes=20),
    )
    store.set_match_state(
        SourceId("rne"),
        "rne-asset-1",
        status=MatchStatus.PENDING,
        diagnostics="stale pending state",
        updated_at=base + timedelta(minutes=10),
    )
    match = store.get_match_state(SourceId("rne"), "rne-asset-1")
    assert match is not None
    assert match.status is MatchStatus.MATCHED
    assert match.spotify_episode_uri == "spotify:episode:newer"

    store.record_source_run(
        SourceId("rne"),
        started_at=base,
        finished_at=base + timedelta(minutes=5),
        ok=True,
        edition_count=1,
    )
    store.record_source_run(
        SourceId("rne"),
        started_at=base + timedelta(minutes=20),
        finished_at=base + timedelta(minutes=21),
        ok=False,
        edition_count=0,
        error="newer failure",
    )
    store.record_source_run(
        SourceId("rne"),
        started_at=base + timedelta(minutes=10),
        finished_at=base + timedelta(minutes=11),
        ok=True,
        edition_count=1,
    )
    source_state = store.get_source_state(SourceId("rne"))
    assert source_state is not None
    assert source_state.last_attempt_at == base + timedelta(minutes=21)
    assert source_state.last_success_at == base + timedelta(minutes=11)
    assert source_state.last_error == "newer failure"

    store.record_playlist_run(
        PlaylistId("spain"),
        started_at=base + timedelta(minutes=20),
        finished_at=base + timedelta(minutes=21),
        ok=False,
        desired_count=10,
        applied_count=0,
        error="newer reconciliation failure",
    )
    store.record_playlist_run(
        PlaylistId("spain"),
        started_at=base + timedelta(minutes=10),
        finished_at=base + timedelta(minutes=11),
        ok=True,
        desired_count=8,
        applied_count=8,
    )
    playlist_state = store.get_playlist_state(PlaylistId("spain"))
    assert playlist_state is not None
    assert playlist_state.last_attempt_at == base + timedelta(minutes=21)
    assert playlist_state.last_success_at == base + timedelta(minutes=11)
    assert playlist_state.last_error == "newer reconciliation failure"
