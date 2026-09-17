from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import (
    AdapterId,
    CanonicalEdition,
    CountryCode,
    DestinationReference,
    EngineConfig,
    ExternalReference,
    LanguageTag,
    ParserId,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.reconciliation import build_desired_state_from_store
from news_bulletin_playlist.snapshot_pending import (
    PendingSnapshotConfirmation,
    PendingSnapshotJournal,
)

BASE = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
AUTH_CONTEXT = "a" * 64
SOURCE_ID = SourceId("rne")
PLAYLIST_ID = PlaylistId("spain")
URI = "spotify:episode:AAAAAAAAAAAAAAAAAAAAAA"


class _Auth:
    def planner_authorization_context(self) -> str:
        return AUTH_CONTEXT

    def get_access_token(self, *, now: datetime | None = None) -> str:
        del now
        raise AssertionError("pending not-before planning must not acquire a provider session")


def _source() -> SourceDefinition:
    return SourceDefinition(
        id=SOURCE_ID,
        display_name="RNE",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url="https://example.test/rne.xml",
        external_references=(
            ExternalReference(system="spotify", resource_type="show", external_id="show"),
        ),
    )


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PLAYLIST_ID,
        display_name="Noticias España",
        description="",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SOURCE_ID,)),
        destination=DestinationReference(AdapterId("spotify"), "destination"),
        retention_hours=48,
    )


def _runner_with_pending(
    tmp_path: Path,
    *,
    readback_exact: bool = True,
    desired_fingerprint: str | None = None,
) -> tuple[EngineRunner, PlaylistDefinition]:
    source = _source()
    playlist = _playlist()
    config = EngineConfig(schema_version=1, sources=(source,), playlists=(playlist,))
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    edition = CanonicalEdition(
        source_id=SOURCE_ID,
        source_native_id="current",
        title="Current bulletin",
        published_at=BASE - timedelta(hours=1),
        edition_at=BASE - timedelta(hours=1),
        duration_seconds=60,
    )
    store.upsert_editions((edition,), observed_at=BASE)
    assert store.set_match_state(
        SOURCE_ID,
        edition.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=URI,
        spotify_duration_seconds=60,
        updated_at=BASE,
    )

    runner = EngineRunner(config, store, _Auth())
    runner._shadow_auth_context = AUTH_CONTEXT
    desired = build_desired_state_from_store(
        store,
        playlist,
        now=BASE,
        source_timezones={SOURCE_ID: source.timezone},
    )
    expected = runner._destination_verification_context(playlist, desired.uris)
    assert expected is not None
    runner.planner_journal.observe_destination(
        playlist_id=str(playlist.id),
        destination_context_fingerprint=expected.destination_context_fingerprint,
        config_fingerprint=expected.config_fingerprint,
        auth_context_fingerprint=expected.auth_context_fingerprint,
        desired_fingerprint=expected.desired_fingerprint,
    )
    runner.planner_journal.verify_destination(
        str(playlist.id),
        expected=expected,
        verified_at=BASE - timedelta(minutes=1),
    )
    runner.planner_journal.begin_destination_verification(str(playlist.id))
    PendingSnapshotJournal(store).set(
        PendingSnapshotConfirmation(
            playlist_id=playlist.id,
            destination_id=playlist.destination.external_id,
            baseline_snapshot_id="snapshot-A",
            expected_snapshot_id="snapshot-B",
            desired_fingerprint=desired_fingerprint or expected.desired_fingerprint,
            created_at=BASE,
            expires_at=BASE + timedelta(hours=1),
            readback_exact=readback_exact,
        )
    )
    return runner, playlist


@pytest.mark.parametrize(
    "elapsed",
    [
        timedelta(seconds=10),
        timedelta(seconds=30),
        timedelta(seconds=59, microseconds=999_000),
    ],
)
def test_exact_pending_does_not_plan_remote_work_before_one_minute(
    tmp_path: Path,
    elapsed: timedelta,
) -> None:
    runner, playlist = _runner_with_pending(tmp_path)

    destination_work, persistence_blocked = runner._plan_destinations(BASE + elapsed, set())

    assert persistence_blocked == set()
    assert playlist.id not in destination_work


def test_exact_pending_becomes_eligible_for_normal_confirmation_at_one_minute(
    tmp_path: Path,
) -> None:
    runner, playlist = _runner_with_pending(tmp_path)

    destination_work, persistence_blocked = runner._plan_destinations(
        BASE + timedelta(seconds=60),
        set(),
    )

    assert persistence_blocked == set()
    assert playlist.id in destination_work


def test_partial_pending_keeps_existing_strict_remote_resolution(tmp_path: Path) -> None:
    runner, playlist = _runner_with_pending(tmp_path, readback_exact=False)

    destination_work, _ = runner._plan_destinations(BASE + timedelta(seconds=10), set())

    assert playlist.id in destination_work


def test_pending_for_another_desired_fingerprint_cannot_defer_remote_work(tmp_path: Path) -> None:
    runner, playlist = _runner_with_pending(tmp_path, desired_fingerprint="b" * 64)

    destination_work, _ = runner._plan_destinations(BASE + timedelta(seconds=10), set())

    assert playlist.id in destination_work


def test_unknown_destination_cannot_use_pending_not_before(tmp_path: Path) -> None:
    runner, playlist = _runner_with_pending(tmp_path)
    runner.planner_journal.mark_destination_unknown(str(playlist.id))

    destination_work, _ = runner._plan_destinations(BASE + timedelta(seconds=10), set())

    assert playlist.id in destination_work


def test_clock_rollback_cannot_use_pending_not_before(tmp_path: Path) -> None:
    runner, playlist = _runner_with_pending(tmp_path)

    destination_work, _ = runner._plan_destinations(BASE - timedelta(microseconds=1), set())

    assert playlist.id in destination_work
