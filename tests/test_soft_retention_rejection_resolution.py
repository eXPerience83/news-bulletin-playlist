from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.models import (
    AdapterId,
    CanonicalEdition,
    CountryCode,
    DestinationReference,
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
from news_bulletin_playlist.soft_retention_diagnostics import SoftRetentionRejectionReason
from news_bulletin_playlist.soft_retention_engine import _resolve_soft_retention_evidence
from news_bulletin_playlist.spotify_destination_audit import playlist_attestation_fingerprint
from news_bulletin_playlist.spotify_planner import (
    DestinationVerificationContext,
    SpotifyPlannerJournal,
    destination_context_fingerprint,
    matching_input_fingerprint,
    playlist_config_fingerprint,
    stable_fingerprint,
)

BASE = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
OBSERVED = BASE + timedelta(hours=2)
AUTH_CONTEXT = "a" * 64
SOURCE_ID = SourceId("source")
PLAYLIST_ID = PlaylistId("playlist")
OLD_URI = "spotify:episode:AAAAAAAAAAAAAAAAAAAAAA"
CURRENT_URI = "spotify:episode:BBBBBBBBBBBBBBBBBBBBBB"


def _source() -> SourceDefinition:
    return SourceDefinition(
        id=SOURCE_ID,
        display_name="Source",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url="https://example.test/news.xml",
        external_references=(
            ExternalReference(system="spotify", resource_type="show", external_id="show"),
        ),
    )


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PLAYLIST_ID,
        display_name="Playlist",
        description="",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SOURCE_ID,)),
        destination=DestinationReference(AdapterId("spotify"), "destination"),
        retention_hours=48,
    )


def _edition(native_id: str, title: str, published_at: datetime) -> CanonicalEdition:
    return CanonicalEdition(
        source_id=SOURCE_ID,
        source_native_id=native_id,
        title=title,
        published_at=published_at,
        edition_at=None,
        duration_seconds=None,
    )


def _prepared_store(tmp_path: Path) -> tuple[SQLiteStore, SourceDefinition, PlaylistDefinition]:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    source = _source()
    playlist = _playlist()
    old = _edition("old", "Old bulletin", BASE - timedelta(hours=47))
    current = _edition("current", "Current bulletin", BASE - timedelta(hours=1))
    store.upsert_editions((old, current), observed_at=BASE)
    for edition, uri in ((old, OLD_URI), (current, CURRENT_URI)):
        assert store.set_match_state(
            edition.source_id,
            edition.source_native_id,
            status=MatchStatus.MATCHED,
            spotify_episode_uri=uri,
            spotify_duration_seconds=300,
            updated_at=BASE,
        )

    journal = SpotifyPlannerJournal(store)
    for edition in (old, current):
        journal.observe_match_input(
            source_id=str(edition.source_id),
            source_native_id=edition.source_native_id,
            input_fingerprint=matching_input_fingerprint(source, edition),
            observed_at=BASE,
        )

    baseline = build_desired_state_from_store(
        store,
        playlist,
        now=BASE,
        source_timezones={SOURCE_ID: source.timezone},
    )
    expected = DestinationVerificationContext(
        desired_fingerprint=stable_fingerprint(baseline.uris),
        destination_context_fingerprint=destination_context_fingerprint(playlist),
        config_fingerprint=playlist_config_fingerprint(playlist),
        auth_context_fingerprint=AUTH_CONTEXT,
    )
    journal.observe_destination(
        playlist_id=str(playlist.id),
        destination_context_fingerprint=expected.destination_context_fingerprint,
        config_fingerprint=expected.config_fingerprint,
        auth_context_fingerprint=expected.auth_context_fingerprint,
        desired_fingerprint=expected.desired_fingerprint,
    )
    journal.verify_destination(str(playlist.id), expected=expected, verified_at=BASE)
    store.set_playlist_attestation(
        playlist.id,
        destination_id=playlist.destination.external_id,
        snapshot_id="snapshot-a",
        desired_fingerprint=playlist_attestation_fingerprint(baseline.uris),
        updated_at=BASE,
    )
    return store, source, playlist


def _resolution(
    store: SQLiteStore,
    source: SourceDefinition,
    playlist: PlaylistDefinition,
):
    logical = build_desired_state_from_store(
        store,
        playlist,
        now=OBSERVED,
        source_timezones={SOURCE_ID: source.timezone},
    )
    return _resolve_soft_retention_evidence(
        store,
        {source.id: source},
        playlist,
        logical,
        now=OBSERVED,
        auth_context_fingerprint=AUTH_CONTEXT,
    )


def test_positive_resolution_preserves_existing_defer_semantics(tmp_path: Path) -> None:
    store, source, playlist = _prepared_store(tmp_path)

    resolution = _resolution(store, source, playlist)

    assert resolution.rejection_reason is None
    assert resolution.evidence is not None
    assert resolution.evidence.decision.should_defer
    assert resolution.evidence.decision.soft_expired_count == 1


def test_resolution_reports_missing_attestation(tmp_path: Path) -> None:
    store, source, playlist = _prepared_store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DELETE FROM spotify_playlist_attestations WHERE playlist_id = ?",
            (str(playlist.id),),
        )

    resolution = _resolution(store, source, playlist)

    assert resolution.evidence is None
    assert resolution.rejection_reason is SoftRetentionRejectionReason.MISSING_ATTESTATION


def test_resolution_reports_dirty_destination(tmp_path: Path) -> None:
    store, source, playlist = _prepared_store(tmp_path)
    SpotifyPlannerJournal(store).begin_destination_verification(str(playlist.id))

    resolution = _resolution(store, source, playlist)

    assert resolution.evidence is None
    assert resolution.rejection_reason is SoftRetentionRejectionReason.DIRTY_DESTINATION


def test_resolution_reports_missing_match_input(tmp_path: Path) -> None:
    store, source, playlist = _prepared_store(tmp_path)
    SpotifyPlannerJournal(store).clear_match_input(str(SOURCE_ID), "old")

    resolution = _resolution(store, source, playlist)

    assert resolution.evidence is None
    assert resolution.rejection_reason is SoftRetentionRejectionReason.MATCHING_INPUT_MISSING


def test_resolution_prefers_persistence_barrier_over_generic_missing_input(
    tmp_path: Path,
) -> None:
    store, source, playlist = _prepared_store(tmp_path)
    journal = SpotifyPlannerJournal(store)
    journal.clear_match_input(str(SOURCE_ID), "old")
    durable = store.get_match_state(SOURCE_ID, "old")
    assert durable is not None
    journal.record_persistence_rejection_barrier(durable, rejected_at=OBSERVED)

    resolution = _resolution(store, source, playlist)

    assert resolution.evidence is None
    assert (
        resolution.rejection_reason
        is SoftRetentionRejectionReason.MATCHING_PERSISTENCE_REJECTION_BARRIER
    )


@pytest.mark.parametrize(
    ("attempted_at", "expected_reason"),
    [
        (OBSERVED, SoftRetentionRejectionReason.MATCHING_RECOVERY_BACKOFF),
        (OBSERVED - timedelta(hours=7), SoftRetentionRejectionReason.MATCHING_RECOVERY_DUE),
    ],
)
def test_resolution_distinguishes_matching_recovery_phase(
    tmp_path: Path,
    attempted_at: datetime,
    expected_reason: SoftRetentionRejectionReason,
) -> None:
    store, source, playlist = _prepared_store(tmp_path)
    journal = SpotifyPlannerJournal(store)
    journal.clear_match_input(str(SOURCE_ID), "old")
    old = store.get_edition(SOURCE_ID, "old")
    assert old is not None
    journal.record_matched_recovery(
        source_id=str(SOURCE_ID),
        source_native_id="old",
        input_fingerprint=matching_input_fingerprint(source, old),
        attempted_at=attempted_at,
    )

    resolution = _resolution(store, source, playlist)

    assert resolution.evidence is None
    assert resolution.rejection_reason is expected_reason


def test_resolution_reports_unknown_destination(tmp_path: Path) -> None:
    store, source, playlist = _prepared_store(tmp_path)
    SpotifyPlannerJournal(store).mark_destination_unknown(str(playlist.id))

    resolution = _resolution(store, source, playlist)

    assert resolution.evidence is None
    assert resolution.rejection_reason is SoftRetentionRejectionReason.UNKNOWN_DESTINATION
