from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import news_bulletin_playlist.engine as engine_module
from news_bulletin_playlist.desired_state import DesiredPlaylistItem, DesiredPlaylistState
from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import (
    AdapterId,
    CountryCode,
    DestinationReference,
    LanguageTag,
    PlaylistDefinition,
    PlaylistId,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.reconciliation import PlaylistReconciliationResult
from news_bulletin_playlist.snapshot_pending import (
    PendingSnapshotConfirmation,
    PendingSnapshotJournal,
)
from news_bulletin_playlist.spotify.first_desired import (
    SpotifyFirstDesiredBuild,
    desired_fingerprint,
)
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify_backoff import ProviderAuthorizationSession

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
PLAYLIST_ID = PlaylistId("spain_spanish_news")
DESTINATION_ID = "spotify-destination"
FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def _journal(store: SQLiteStore) -> SpotifyFirstJournal:
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    return journal


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PLAYLIST_ID,
        display_name="Noticias España",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SourceId("rne"),)),
        destination=DestinationReference(AdapterId("spotify"), DESTINATION_ID),
    )


def _lease_runner(store: SQLiteStore, journal: SpotifyFirstJournal) -> EngineRunner:
    runner = object.__new__(EngineRunner)
    runner.store = store
    runner.first_journal = journal
    runner.destination_audit = timedelta(hours=6)
    runner.degraded_destination_audit = timedelta(minutes=30)
    return runner


def _persist_degraded_evidence(
    store: SQLiteStore,
    journal: SpotifyFirstJournal,
    *,
    fingerprint: str = FINGERPRINT,
    verified_at: datetime = NOW,
) -> None:
    store.set_playlist_attestation(
        PLAYLIST_ID,
        destination_id=DESTINATION_ID,
        snapshot_id="snapshot-A",
        desired_fingerprint=fingerprint,
        updated_at=verified_at,
    )
    journal.record_degraded_destination_verification(
        PLAYLIST_ID,
        destination_id=DESTINATION_ID,
        desired_fingerprint=fingerprint,
        verified_at=verified_at,
    )


def _build() -> SpotifyFirstDesiredBuild:
    desired = DesiredPlaylistState(
        playlist_id=PLAYLIST_ID,
        generated_at=NOW,
        items=(
            DesiredPlaylistItem(
                source_id=SourceId("rne"),
                source_native_id="episode-1",
                spotify_episode_uri="spotify:episode:1234567890123456789012",
            ),
        ),
    )
    return SpotifyFirstDesiredBuild(desired=desired, duration_unavailable_count=0)


def test_degraded_destination_lease_survives_restart_and_expires(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = _journal(store)
    _persist_degraded_evidence(store, journal)
    runner = _lease_runner(store, journal)

    assert (
        runner._destination_skip_evidence(
            _playlist(),
            FINGERPRINT,
            now=NOW + timedelta(minutes=10),
        )
        == "degraded"
    )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_journal = _journal(restarted_store)
    restarted = _lease_runner(restarted_store, restarted_journal)

    assert (
        restarted._destination_skip_evidence(
            _playlist(),
            FINGERPRINT,
            now=NOW + timedelta(minutes=20),
        )
        == "degraded"
    )
    assert (
        restarted._destination_skip_evidence(
            _playlist(),
            FINGERPRINT,
            now=NOW + timedelta(minutes=30),
        )
        is None
    )


def test_degraded_destination_lease_requires_matching_attestation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = _journal(store)
    journal.record_degraded_destination_verification(
        PLAYLIST_ID,
        destination_id=DESTINATION_ID,
        desired_fingerprint=FINGERPRINT,
        verified_at=NOW,
    )
    runner = _lease_runner(store, journal)

    assert (
        runner._destination_skip_evidence(
            _playlist(),
            FINGERPRINT,
            now=NOW + timedelta(minutes=10),
        )
        is None
    )
    assert journal.get_degraded_destination_verification(PLAYLIST_ID) is None


def test_desired_mismatch_invalidates_degraded_destination_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = _journal(store)
    _persist_degraded_evidence(store, journal)
    runner = _lease_runner(store, journal)

    assert (
        runner._destination_skip_evidence(
            _playlist(),
            OTHER_FINGERPRINT,
            now=NOW + timedelta(minutes=10),
        )
        is None
    )
    assert journal.get_degraded_destination_verification(PLAYLIST_ID) is None


def test_pending_transition_invalidates_degraded_destination_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = _journal(store)
    _persist_degraded_evidence(store, journal)
    PendingSnapshotJournal(store).set(
        PendingSnapshotConfirmation(
            playlist_id=PLAYLIST_ID,
            destination_id=DESTINATION_ID,
            baseline_snapshot_id="snapshot-A",
            expected_snapshot_id="snapshot-B",
            desired_fingerprint=FINGERPRINT,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            readback_exact=True,
        )
    )
    runner = _lease_runner(store, journal)

    assert (
        runner._destination_skip_evidence(
            _playlist(),
            FINGERPRINT,
            now=NOW + timedelta(minutes=10),
        )
        is None
    )
    assert journal.get_degraded_destination_verification(PLAYLIST_ID) is None


def test_exact_destination_verification_takes_precedence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = _journal(store)
    _persist_degraded_evidence(store, journal)
    journal.record_destination_verification(
        PLAYLIST_ID,
        destination_id=DESTINATION_ID,
        desired_fingerprint=FINGERPRINT,
        verified_at=NOW,
    )
    runner = _lease_runner(store, journal)

    assert (
        runner._destination_skip_evidence(
            _playlist(),
            FINGERPRINT,
            now=NOW + timedelta(minutes=10),
        )
        == "exact"
    )


def test_qualified_degraded_reconciliation_renews_only_degraded_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    journal = _journal(store)
    runner = _lease_runner(store, journal)
    runner.playlist_mutation_guard = None
    build = _build()
    fingerprint = desired_fingerprint(build.desired.uris)

    store.set_playlist_attestation(
        PLAYLIST_ID,
        destination_id=DESTINATION_ID,
        snapshot_id="snapshot-A",
        desired_fingerprint=fingerprint,
        updated_at=NOW,
    )

    monkeypatch.setattr(
        engine_module,
        "reconcile_spotify_playlist",
        lambda *args, **kwargs: PlaylistReconciliationResult(
            playlist_id=PLAYLIST_ID,
            ok=True,
            desired_count=1,
            applied_count=1,
            wrote=False,
            degraded_verification=True,
            warning="Spotify playlist contains unavailable media at indices [0]",
            degraded_evidence_qualified=True,
        ),
    )

    outcome = runner._reconcile_playlist(
        _playlist(),
        build,
        fingerprint,
        object(),  # type: ignore[arg-type]
        ProviderAuthorizationSession("token", "auth-context"),
        now=NOW,
    )

    assert outcome.ok is True
    assert outcome.wrote is False
    assert outcome.destination_verification_kind == "degraded"
    assert journal.get_destination_verification(PLAYLIST_ID) is None
    degraded = journal.get_degraded_destination_verification(PLAYLIST_ID)
    assert degraded is not None
    assert degraded.desired_fingerprint == fingerprint


def test_exact_reconciliation_clears_degraded_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    journal = _journal(store)
    build = _build()
    fingerprint = desired_fingerprint(build.desired.uris)
    _persist_degraded_evidence(store, journal, fingerprint=fingerprint)

    runner = _lease_runner(store, journal)
    runner.playlist_mutation_guard = None

    monkeypatch.setattr(
        engine_module,
        "reconcile_spotify_playlist",
        lambda *args, **kwargs: PlaylistReconciliationResult(
            playlist_id=PLAYLIST_ID,
            ok=True,
            desired_count=1,
            applied_count=1,
            wrote=False,
            remote_evidence_qualified=True,
        ),
    )

    outcome = runner._reconcile_playlist(
        _playlist(),
        build,
        fingerprint,
        object(),  # type: ignore[arg-type]
        ProviderAuthorizationSession("token", "auth-context"),
        now=NOW + timedelta(minutes=30),
    )

    assert outcome.destination_verification_kind == "exact"
    assert journal.get_degraded_destination_verification(PLAYLIST_ID) is None
    exact = journal.get_destination_verification(PLAYLIST_ID)
    assert exact is not None
    assert exact.desired_fingerprint == fingerprint
