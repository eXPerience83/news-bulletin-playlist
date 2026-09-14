from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from news_bulletin_playlist.desired_state import DesiredPlaylistState
from news_bulletin_playlist.models import (
    AdapterId,
    CanonicalEdition,
    CountryCode,
    DestinationReference,
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
from news_bulletin_playlist.soft_retention_context import bind_soft_retention_audit_resolver
from news_bulletin_playlist.soft_retention_engine import resolve_soft_retention_evidence
from news_bulletin_playlist.spotify_destination_audit import (
    audit_spotify_playlist_snapshot_first,
    playlist_attestation_fingerprint,
)
from news_bulletin_playlist.spotify_planner import (
    DestinationVerificationContext,
    SpotifyPlannerJournal,
    destination_context_fingerprint,
    matching_input_fingerprint,
    playlist_config_fingerprint,
    stable_fingerprint,
)

NOW = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)
AUTH_CONTEXT = stable_fingerprint(("auth",))
SOURCE_ID = SourceId("source")
PLAYLIST_ID = PlaylistId("playlist")
URI_CURRENT = "spotify:episode:AAAAAAAAAAAAAAAAAAAAAA"
URI_EXPIRED = "spotify:episode:BBBBBBBBBBBBBBBBBBBBBB"
URI_NEW = "spotify:episode:CCCCCCCCCCCCCCCCCCCCCC"


def _source() -> SourceDefinition:
    return SourceDefinition(
        id=SOURCE_ID,
        display_name="Source",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("test"),
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


def _edition(native_id: str, *, age: timedelta) -> CanonicalEdition:
    published = NOW - age
    return CanonicalEdition(
        source_id=SOURCE_ID,
        source_native_id=native_id,
        title=native_id,
        published_at=published,
        edition_at=published,
        duration_seconds=300,
    )


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def _prepare_baseline(
    tmp_path: Path,
    *,
    attested_at: datetime,
    current_age: timedelta,
    expired_age: timedelta,
) -> tuple[
    SQLiteStore,
    SourceDefinition,
    PlaylistDefinition,
    DesiredPlaylistState,
]:
    store = _store(tmp_path)
    source = _source()
    playlist = _playlist()
    current = _edition("current", age=current_age)
    expired = _edition("expired", age=expired_age)
    store.upsert_editions((current, expired), observed_at=attested_at - timedelta(minutes=2))
    for edition, uri in ((current, URI_CURRENT), (expired, URI_EXPIRED)):
        store.set_match_state(
            edition.source_id,
            edition.source_native_id,
            status=MatchStatus.MATCHED,
            spotify_episode_uri=uri,
            spotify_duration_seconds=300,
            updated_at=attested_at - timedelta(minutes=1),
        )

    baseline = build_desired_state_from_store(
        store,
        playlist,
        now=attested_at,
        source_timezones={SOURCE_ID: source.timezone},
    )
    journal = SpotifyPlannerJournal(store)
    for edition in (current, expired):
        journal.observe_match_input(
            source_id=str(edition.source_id),
            source_native_id=edition.source_native_id,
            input_fingerprint=matching_input_fingerprint(source, edition),
            observed_at=attested_at - timedelta(minutes=1),
        )

    destination_context = destination_context_fingerprint(playlist)
    config_fingerprint = playlist_config_fingerprint(playlist)
    desired_fingerprint = stable_fingerprint(baseline.uris)
    journal.observe_destination(
        playlist_id=str(playlist.id),
        destination_context_fingerprint=destination_context,
        config_fingerprint=config_fingerprint,
        auth_context_fingerprint=AUTH_CONTEXT,
        desired_fingerprint=desired_fingerprint,
    )
    journal.verify_destination(
        str(playlist.id),
        expected=DestinationVerificationContext(
            desired_fingerprint=desired_fingerprint,
            destination_context_fingerprint=destination_context,
            config_fingerprint=config_fingerprint,
            auth_context_fingerprint=AUTH_CONTEXT,
        ),
        verified_at=attested_at,
    )
    store.set_playlist_attestation(
        playlist.id,
        destination_id=playlist.destination.external_id,
        snapshot_id="snapshot-a",
        desired_fingerprint=playlist_attestation_fingerprint(baseline.uris),
        updated_at=attested_at,
    )
    return store, source, playlist, baseline


def test_durable_evidence_proves_expiry_only_transition(tmp_path: Path) -> None:
    attested_at = NOW - timedelta(hours=2)
    store, source, playlist, baseline = _prepare_baseline(
        tmp_path,
        attested_at=attested_at,
        current_age=timedelta(hours=3),
        expired_age=timedelta(hours=49),
    )
    logical = build_desired_state_from_store(
        store,
        playlist,
        now=NOW,
        source_timezones={SOURCE_ID: source.timezone},
    )

    evidence = resolve_soft_retention_evidence(
        store,
        {SOURCE_ID: source},
        playlist,
        logical,
        now=NOW,
        auth_context_fingerprint=AUTH_CONTEXT,
    )

    assert evidence is not None
    assert evidence.physical_baseline == baseline
    assert evidence.decision.should_defer
    assert evidence.decision.soft_expired_count == 1


def test_matched_recovery_state_never_substitutes_for_match_input(tmp_path: Path) -> None:
    attested_at = NOW - timedelta(hours=2)
    store, source, playlist, _ = _prepare_baseline(
        tmp_path,
        attested_at=attested_at,
        current_age=timedelta(hours=3),
        expired_age=timedelta(hours=49),
    )
    journal = SpotifyPlannerJournal(store)
    expired = store.get_edition(SOURCE_ID, "expired")
    assert expired is not None
    fingerprint = matching_input_fingerprint(source, expired)
    journal.clear_match_input(str(SOURCE_ID), "expired")
    journal.record_matched_recovery(
        source_id=str(SOURCE_ID),
        source_native_id="expired",
        input_fingerprint=fingerprint,
        attempted_at=NOW - timedelta(minutes=10),
    )
    logical = build_desired_state_from_store(
        store,
        playlist,
        now=NOW,
        source_timezones={SOURCE_ID: source.timezone},
    )

    assert (
        resolve_soft_retention_evidence(
            store,
            {SOURCE_ID: source},
            playlist,
            logical,
            now=NOW,
            auth_context_fingerprint=AUTH_CONTEXT,
        )
        is None
    )


def test_clock_rollback_after_newer_verification_rejects_deferral(tmp_path: Path) -> None:
    attested_at = NOW - timedelta(hours=3)
    store, source, playlist, baseline = _prepare_baseline(
        tmp_path,
        attested_at=attested_at,
        current_age=timedelta(hours=4),
        expired_age=timedelta(hours=49),
    )
    journal = SpotifyPlannerJournal(store)
    expected = DestinationVerificationContext(
        desired_fingerprint=stable_fingerprint(baseline.uris),
        destination_context_fingerprint=destination_context_fingerprint(playlist),
        config_fingerprint=playlist_config_fingerprint(playlist),
        auth_context_fingerprint=AUTH_CONTEXT,
    )
    journal.verify_destination(
        str(playlist.id),
        expected=expected,
        verified_at=NOW - timedelta(hours=1),
    )
    rolled_back_now = NOW - timedelta(hours=2)
    logical = build_desired_state_from_store(
        store,
        playlist,
        now=rolled_back_now,
        source_timezones={SOURCE_ID: source.timezone},
    )

    assert (
        resolve_soft_retention_evidence(
            store,
            {SOURCE_ID: source},
            playlist,
            logical,
            now=rolled_back_now,
            auth_context_fingerprint=AUTH_CONTEXT,
        )
        is None
    )


def test_new_episode_rejects_expiry_only_evidence(tmp_path: Path) -> None:
    attested_at = NOW - timedelta(hours=2)
    store, source, playlist, _ = _prepare_baseline(
        tmp_path,
        attested_at=attested_at,
        current_age=timedelta(hours=3),
        expired_age=timedelta(hours=49),
    )
    new = _edition("new", age=timedelta(minutes=5))
    store.upsert_editions((new,), observed_at=NOW)
    store.set_match_state(
        new.source_id,
        new.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=URI_NEW,
        spotify_duration_seconds=300,
        updated_at=NOW,
    )
    logical = build_desired_state_from_store(
        store,
        playlist,
        now=NOW,
        source_timezones={SOURCE_ID: source.timezone},
    )

    assert (
        resolve_soft_retention_evidence(
            store,
            {SOURCE_ID: source},
            playlist,
            logical,
            now=NOW,
            auth_context_fingerprint=AUTH_CONTEXT,
        )
        is None
    )


def test_mapping_changed_after_attestation_rejects_deferral(tmp_path: Path) -> None:
    attested_at = NOW - timedelta(hours=2)
    store, source, playlist, _ = _prepare_baseline(
        tmp_path,
        attested_at=attested_at,
        current_age=timedelta(hours=3),
        expired_age=timedelta(hours=49),
    )
    store.set_match_state(
        SOURCE_ID,
        "expired",
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:DDDDDDDDDDDDDDDDDDDDDD",
        spotify_duration_seconds=300,
        updated_at=NOW,
    )
    logical = build_desired_state_from_store(
        store,
        playlist,
        now=NOW,
        source_timezones={SOURCE_ID: source.timezone},
    )

    assert (
        resolve_soft_retention_evidence(
            store,
            {SOURCE_ID: source},
            playlist,
            logical,
            now=NOW,
            auth_context_fingerprint=AUTH_CONTEXT,
        )
        is None
    )


def test_hard_56_hour_boundary_forces_cleanup(tmp_path: Path) -> None:
    attested_at = NOW - timedelta(hours=10, minutes=1)
    store, source, playlist, _ = _prepare_baseline(
        tmp_path,
        attested_at=attested_at,
        current_age=timedelta(hours=12),
        expired_age=timedelta(hours=56, minutes=1),
    )
    logical = build_desired_state_from_store(
        store,
        playlist,
        now=NOW,
        source_timezones={SOURCE_ID: source.timezone},
    )

    evidence = resolve_soft_retention_evidence(
        store,
        {SOURCE_ID: source},
        playlist,
        logical,
        now=NOW,
        auth_context_fingerprint=AUTH_CONTEXT,
    )

    assert evidence is not None
    assert evidence.decision.force_cleanup
    assert not evidence.decision.should_defer


class _Spotify:
    def __init__(self, uris: tuple[str, ...], snapshots: list[str]) -> None:
        self.uris = list(uris)
        self.snapshots = snapshots
        self.snapshot_calls = 0
        self.item_calls = 0

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        del playlist_id
        index = min(self.snapshot_calls, len(self.snapshots) - 1)
        self.snapshot_calls += 1
        return {"snapshot_id": self.snapshots[index]}

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del playlist_id
        self.item_calls += 1
        page = self.uris[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in page],
            "next": "next" if offset + len(page) < len(self.uris) else None,
            "total": len(self.uris),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        del playlist_id, uris
        raise AssertionError("soft-retention audit must never write")


def test_six_hour_audit_verifies_deferred_physical_baseline_without_cleanup(
    tmp_path: Path,
) -> None:
    attested_at = NOW - timedelta(hours=2)
    store, source, playlist, baseline = _prepare_baseline(
        tmp_path,
        attested_at=attested_at,
        current_age=timedelta(hours=3),
        expired_age=timedelta(hours=49),
    )
    logical = build_desired_state_from_store(
        store,
        playlist,
        now=NOW,
        source_timezones={SOURCE_ID: source.timezone},
    )
    client = _Spotify(baseline.uris, ["snapshot-a"])

    with bind_soft_retention_audit_resolver(
        lambda candidate_store, candidate_playlist, candidate_desired, observed_at: (
            baseline
            if candidate_store is store
            and candidate_playlist == playlist
            and candidate_desired == logical
            and observed_at == NOW
            else None
        )
    ):
        result = audit_spotify_playlist_snapshot_first(
            client,
            playlist,
            logical,
            store=store,
            observed_at=NOW,
        )

    assert result is not None and result.remote_evidence_qualified
    assert result.desired_count == len(logical.items)
    assert result.applied_count == len(baseline.items)
    assert result.verified_desired_uris == baseline.uris
    assert result.wrote is False
    assert client.snapshot_calls == 1
    assert client.item_calls == 0


def test_metadata_snapshot_audit_does_not_move_deferred_baseline_timestamp(
    tmp_path: Path,
) -> None:
    attested_at = NOW - timedelta(hours=2)
    store, source, playlist, baseline = _prepare_baseline(
        tmp_path,
        attested_at=attested_at,
        current_age=timedelta(hours=3),
        expired_age=timedelta(hours=49),
    )
    logical = build_desired_state_from_store(
        store,
        playlist,
        now=NOW,
        source_timezones={SOURCE_ID: source.timezone},
    )
    client = _Spotify(baseline.uris, ["snapshot-c", "snapshot-c"])

    with bind_soft_retention_audit_resolver(lambda *_args: baseline):
        result = audit_spotify_playlist_snapshot_first(
            client,
            playlist,
            logical,
            store=store,
            observed_at=NOW,
        )

    assert result is not None and result.remote_evidence_qualified
    assert client.snapshot_calls == 2
    assert client.item_calls == 1
    attestation = store.get_playlist_attestation(playlist.id)
    assert attestation is not None
    assert attestation.snapshot_id == "snapshot-a"
    assert attestation.updated_at == attested_at
