from __future__ import annotations

import sqlite3
from dataclasses import replace
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
from news_bulletin_playlist.persistence import MatchStatus, PersistenceError, SQLiteStore
from news_bulletin_playlist.spotify_planner import (
    AUDIT_INTERVAL,
    DestinationVerificationContext,
    PlannerReason,
    SpotifyPlannerJournal,
    destination_context_fingerprint,
    edition_input_fingerprint,
    matched_recovery_planning_reason,
    matching_input_fingerprint,
    plan_shadow_cycle,
    playlist_config_fingerprint,
    source_contract_fingerprint,
    stable_fingerprint,
    unresolved_planning_reason,
    unresolved_retry_interval,
)

NOW = datetime(2026, 9, 8, 10, 30, 45, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "planner.sqlite3")
    store.initialize()
    return store


def _source(show_id: str = "show-one") -> SourceDefinition:
    return SourceDefinition(
        id=SourceId("source"),
        display_name="Source",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ser"),
        endpoint_url="https://provider.invalid/feed.xml",
        external_references=(ExternalReference("spotify", "show", show_id),),
    )


def _playlist(destination: str = "destination-one", retention: int = 48) -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId("playlist"),
        display_name="Playlist",
        description="Description",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SourceId("source"),)),
        destination=DestinationReference(AdapterId("spotify"), destination),
        retention_hours=retention,
    )


def _edition(*, title: str = "Bulletin") -> CanonicalEdition:
    return CanonicalEdition(
        source_id=SourceId("source"),
        source_native_id="edition-one",
        title=title,
        published_at=NOW - timedelta(minutes=5),
        edition_at=NOW - timedelta(minutes=30),
        duration_seconds=60,
    )


def _observe(journal: SpotifyPlannerJournal, desired: str = SHA_A):
    return journal.observe_destination(
        playlist_id="playlist",
        destination_context_fingerprint=SHA_A,
        config_fingerprint=SHA_A,
        auth_context_fingerprint=SHA_A,
        desired_fingerprint=desired,
    )


def test_bootstrap_and_corrupt_state_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = SpotifyPlannerJournal(store)
    state = _observe(journal)
    result = plan_shadow_cycle(
        source_reasons=(),
        destination_states=(state,),
        catalogue_states=(None,),
        pending_verification_count=0,
        now=NOW,
        additional_reasons=(PlannerReason.MISSING_EVIDENCE,),
    )
    assert result.would_need_spotify
    assert not result.would_allow_local_skip
    assert PlannerReason.MISSING_EVIDENCE in result.reasons

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """UPDATE spotify_planner_destination_state
               SET config_fingerprint = 'invalid' WHERE playlist_id = ?""",
            ("playlist",),
        )
    with pytest.raises(PersistenceError):
        journal.get_destination("playlist")

    with pytest.raises(PersistenceError):
        journal.observe_match_input(
            source_id="missing-source",
            source_native_id="missing-edition",
            input_fingerprint=SHA_A,
            observed_at=NOW,
        )


def test_dirty_and_audit_due_are_independent_and_verification_survives_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    journal = SpotifyPlannerJournal(store)
    dirty = _observe(journal)
    journal.verify_catalogue(SHA_A, SHA_A, verified_at=NOW)
    clean_catalogue = journal.get_catalogue(SHA_A)
    dirty_result = plan_shadow_cycle(
        source_reasons=(),
        destination_states=(dirty,),
        catalogue_states=(clean_catalogue,),
        pending_verification_count=0,
        now=NOW,
    )
    assert PlannerReason.DIRTY in dirty_result.reasons
    assert PlannerReason.CATALOGUE_AUDIT_DUE not in dirty_result.reasons

    journal.verify_destination(
        "playlist",
        expected=DestinationVerificationContext(SHA_A, SHA_A, SHA_A, SHA_A),
        verified_at=NOW,
    )
    restarted = SpotifyPlannerJournal(SQLiteStore(store.path))
    clean = restarted.get_destination("playlist")
    assert clean is not None
    assert clean.next_audit_at == NOW + AUDIT_INTERVAL
    audit_result = plan_shadow_cycle(
        source_reasons=(),
        destination_states=(clean,),
        catalogue_states=(clean_catalogue,),
        pending_verification_count=0,
        now=NOW + AUDIT_INTERVAL,
    )
    assert PlannerReason.DIRTY not in audit_result.reasons
    assert PlannerReason.DESTINATION_AUDIT_DUE in audit_result.reasons


def test_local_observation_does_not_extend_audit_and_clock_rollback_is_due(
    tmp_path: Path,
) -> None:
    journal = SpotifyPlannerJournal(_store(tmp_path))
    _observe(journal)
    journal.verify_destination(
        "playlist",
        expected=DestinationVerificationContext(SHA_A, SHA_A, SHA_A, SHA_A),
        verified_at=NOW,
    )
    observed = _observe(journal)
    assert observed.next_audit_at == NOW + AUDIT_INTERVAL
    rolled_back = plan_shadow_cycle(
        source_reasons=(),
        destination_states=(observed,),
        catalogue_states=(),
        pending_verification_count=0,
        now=NOW - timedelta(seconds=1),
    )
    assert PlannerReason.DESTINATION_AUDIT_DUE in rolled_back.reasons


def test_unknown_a_to_b_to_a_never_revives_old_confirmation(tmp_path: Path) -> None:
    journal = SpotifyPlannerJournal(_store(tmp_path))
    _observe(journal, SHA_A)
    journal.verify_destination(
        "playlist",
        expected=DestinationVerificationContext(SHA_A, SHA_A, SHA_A, SHA_A),
        verified_at=NOW,
    )
    journal.begin_destination_verification("playlist")
    in_flight = journal.get_destination("playlist")
    assert in_flight is not None
    assert in_flight.dirty
    assert in_flight.confirmed_desired_fingerprint is None
    changed = _observe(journal, SHA_B)
    assert changed.confirmed_desired_fingerprint is None
    journal.mark_destination_unknown("playlist")
    returned = _observe(journal, SHA_A)
    assert returned.unknown
    assert returned.dirty
    assert returned.confirmed_desired_fingerprint is None


def test_retention_change_in_desired_fingerprint_marks_destination_dirty(tmp_path: Path) -> None:
    journal = SpotifyPlannerJournal(_store(tmp_path))
    journal.observe_destination(
        playlist_id="playlist",
        destination_context_fingerprint=SHA_A,
        config_fingerprint=SHA_A,
        auth_context_fingerprint=SHA_A,
        desired_fingerprint=stable_fingerprint(("first", "expiring")),
    )
    journal.verify_destination(
        "playlist",
        expected=DestinationVerificationContext(
            stable_fingerprint(("first", "expiring")), SHA_A, SHA_A, SHA_A
        ),
        verified_at=NOW,
    )
    retained = journal.observe_destination(
        playlist_id="playlist",
        destination_context_fingerprint=SHA_A,
        config_fingerprint=SHA_A,
        auth_context_fingerprint=SHA_A,
        desired_fingerprint=stable_fingerprint(("first",)),
    )
    assert retained.dirty
    assert retained.confirmed_desired_fingerprint is None


def test_destination_or_configuration_context_change_invalidates_evidence(tmp_path: Path) -> None:
    journal = SpotifyPlannerJournal(_store(tmp_path))
    _observe(journal)
    journal.verify_destination(
        "playlist",
        expected=DestinationVerificationContext(SHA_A, SHA_A, SHA_A, SHA_A),
        verified_at=NOW,
    )
    changed = journal.observe_destination(
        playlist_id="playlist",
        destination_context_fingerprint=SHA_B,
        config_fingerprint=SHA_B,
        auth_context_fingerprint=SHA_A,
        desired_fingerprint=SHA_A,
    )
    assert changed.dirty
    assert changed.confirmed_desired_fingerprint is None
    assert changed.last_verified_at is None
    assert changed.next_audit_at is None


@pytest.mark.parametrize(
    ("age", "interval"),
    [
        (timedelta(minutes=59), timedelta(minutes=15)),
        (timedelta(hours=1), timedelta(minutes=30)),
        (timedelta(hours=3), timedelta(hours=1)),
        (timedelta(hours=6), timedelta(hours=6)),
    ],
)
def test_adaptive_unresolved_retry_schedule(age: timedelta, interval: timedelta) -> None:
    assert unresolved_retry_interval(age) == interval


def test_unresolved_restart_new_edition_and_input_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_editions((_edition(),), observed_at=NOW)
    journal = SpotifyPlannerJournal(store)
    journal.observe_match_input(
        source_id="source",
        source_native_id="edition-one",
        input_fingerprint=edition_input_fingerprint(_edition()),
        observed_at=NOW,
    )
    first = journal.record_unresolved(
        source_id="source",
        source_native_id="edition-one",
        input_fingerprint=SHA_A,
        status=MatchStatus.PENDING,
        attempted_at=NOW,
    )
    restarted = SpotifyPlannerJournal(SQLiteStore(store.path))
    assert restarted.get_unresolved("source", "edition-one") == first
    match_input = restarted.get_match_input("source", "edition-one")
    assert match_input is not None
    assert match_input.input_fingerprint == edition_input_fingerprint(_edition())

    changed = restarted.record_unresolved(
        source_id="source",
        source_native_id="edition-one",
        input_fingerprint=SHA_B,
        status=MatchStatus.AMBIGUOUS,
        attempted_at=NOW + timedelta(hours=2),
    )
    assert changed.first_unresolved_at == NOW + timedelta(hours=2)
    assert changed.next_retry_at == NOW + timedelta(hours=2, minutes=15)

    second_edition = CanonicalEdition(
        source_id=SourceId("source"),
        source_native_id="edition-two",
        title="Second",
        published_at=NOW,
        edition_at=NOW,
    )
    store.upsert_editions((second_edition,), observed_at=NOW)
    second = restarted.record_unresolved(
        source_id="source",
        source_native_id="edition-two",
        input_fingerprint=SHA_A,
        status=MatchStatus.PENDING,
        attempted_at=NOW + timedelta(hours=8),
    )
    assert second.first_unresolved_at == NOW + timedelta(hours=8)
    assert second.next_retry_at == NOW + timedelta(hours=8, minutes=15)
    assert (
        unresolved_planning_reason(first, input_fingerprint=SHA_A, now=NOW - timedelta(seconds=1))
        is PlannerReason.CLOCK_ROLLBACK
    )
    assert (
        unresolved_planning_reason(first, input_fingerprint=SHA_A, now=first.next_retry_at)
        is PlannerReason.RETRY_DUE
    )


def test_matched_recovery_is_durable_non_authoritative_and_six_hour_bounded(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    edition = _edition()
    store.upsert_editions((edition,), observed_at=NOW)
    journal = SpotifyPlannerJournal(store)

    first = journal.record_matched_recovery(
        source_id="source",
        source_native_id="edition-one",
        input_fingerprint=SHA_A,
        attempted_at=NOW,
    )

    assert first.first_attempt_at == NOW
    assert first.last_attempt_at == NOW
    assert first.next_retry_at == NOW + AUDIT_INTERVAL
    assert journal.get_match_input("source", "edition-one") is None
    restarted = SpotifyPlannerJournal(SQLiteStore(store.path))
    assert restarted.get_matched_recovery("source", "edition-one") == first
    assert (
        matched_recovery_planning_reason(
            first,
            input_fingerprint=SHA_A,
            now=NOW + timedelta(minutes=10),
        )
        is None
    )
    assert (
        matched_recovery_planning_reason(
            first,
            input_fingerprint=SHA_A,
            now=first.next_retry_at,
        )
        is PlannerReason.RETRY_DUE
    )
    assert (
        matched_recovery_planning_reason(
            first,
            input_fingerprint=SHA_B,
            now=NOW + timedelta(minutes=10),
        )
        is PlannerReason.INPUT_CHANGED
    )
    assert (
        matched_recovery_planning_reason(
            first,
            input_fingerprint=SHA_A,
            now=NOW - timedelta(seconds=1),
        )
        is PlannerReason.CLOCK_ROLLBACK
    )

    refreshed = restarted.record_matched_recovery(
        source_id="source",
        source_native_id="edition-one",
        input_fingerprint=SHA_A,
        attempted_at=NOW + timedelta(hours=6),
    )
    assert refreshed.first_attempt_at == NOW
    assert refreshed.last_attempt_at == NOW + timedelta(hours=6)
    assert refreshed.next_retry_at == NOW + timedelta(hours=12)

    restarted.clear_matched_recovery("source", "edition-one")
    assert restarted.get_matched_recovery("source", "edition-one") is None


def test_semantic_fingerprints_ignore_observation_time_and_invalidate_real_contracts() -> None:
    edition = _edition()
    assert edition_input_fingerprint(edition) == edition_input_fingerprint(_edition())
    assert edition_input_fingerprint(edition) != edition_input_fingerprint(
        _edition(title="Changed")
    )
    assert source_contract_fingerprint(_source()) != source_contract_fingerprint(_source("other"))
    assert matching_input_fingerprint(_source(), edition) != matching_input_fingerprint(
        replace(_source(), spotify_release_delay_days=1), edition
    )
    assert matching_input_fingerprint(_source(), edition) != matching_input_fingerprint(
        replace(_source(), timezone=ZoneInfo("UTC")), edition
    )
    assert playlist_config_fingerprint(_playlist()) != playlist_config_fingerprint(
        _playlist(retention=24)
    )
    assert destination_context_fingerprint(_playlist()) != destination_context_fingerprint(
        _playlist(destination="other")
    )


def test_planner_state_persists_fingerprints_instead_of_sensitive_inputs(tmp_path: Path) -> None:
    sentinels = (
        "access-token-sentinel",
        "refresh-token-sentinel",
        "authorization-code-sentinel",
        "pkce-verifier-sentinel",
        "spotify:episode:episode-uri-sentinel",
    )
    store = _store(tmp_path)
    journal = SpotifyPlannerJournal(store)
    secret_input = " ".join(sentinels)
    fingerprint = stable_fingerprint((secret_input,))
    journal.observe_destination(
        playlist_id="playlist",
        destination_context_fingerprint=fingerprint,
        config_fingerprint=fingerprint,
        auth_context_fingerprint=fingerprint,
        desired_fingerprint=fingerprint,
    )
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            """SELECT destination_context_fingerprint, config_fingerprint,
                      auth_context_fingerprint, observed_desired_fingerprint
               FROM spotify_planner_destination_state"""
        ).fetchone()
    assert row is not None
    serialized = repr(row)
    assert all(sentinel not in serialized for sentinel in sentinels)


def test_pending_unknown_and_due_audit_remain_work_during_cooldown_model() -> None:
    destination = _destination_state(dirty=True, unknown=True, verified=NOW)
    result = plan_shadow_cycle(
        source_reasons=(),
        destination_states=(destination,),
        catalogue_states=(None,),
        pending_verification_count=1,
        now=NOW,
    )
    assert result.would_need_spotify
    assert not result.would_allow_local_skip
    assert PlannerReason.DIRTY in result.reasons
    assert PlannerReason.PENDING_VERIFICATION in result.reasons
    assert PlannerReason.UNKNOWN_MUTATION in result.reasons
    assert PlannerReason.CATALOGUE_AUDIT_DUE in result.reasons


def _destination_state(*, dirty: bool, unknown: bool, verified: datetime | None):
    from news_bulletin_playlist.spotify_planner import DestinationState

    return DestinationState(
        playlist_id="playlist",
        destination_context_fingerprint=SHA_A,
        config_fingerprint=SHA_A,
        auth_context_fingerprint=SHA_A,
        observed_desired_fingerprint=SHA_A,
        confirmed_desired_fingerprint=None if dirty else SHA_A,
        dirty=dirty,
        unknown=unknown,
        last_verified_at=verified,
        next_audit_at=None if verified is None else verified + AUDIT_INTERVAL,
    )


def test_persistence_rejection_barrier_is_durable_and_independent_of_recovery_fingerprint(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    edition = _edition()
    store.upsert_editions((edition,), observed_at=NOW)
    assert store.set_match_state(
        *edition.identity,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:durable-a",
        spotify_duration_seconds=60,
        diagnostics="durable A",
        updated_at=NOW + timedelta(hours=4),
    )
    match = store.get_match_state(*edition.identity)
    assert match is not None
    journal = SpotifyPlannerJournal(store)

    barrier = journal.record_persistence_rejection_barrier(match, rejected_at=NOW)
    journal.record_matched_recovery(
        source_id="source",
        source_native_id="edition-one",
        input_fingerprint=SHA_A,
        attempted_at=NOW,
    )
    changed_recovery = journal.record_matched_recovery(
        source_id="source",
        source_native_id="edition-one",
        input_fingerprint=SHA_B,
        attempted_at=NOW + timedelta(hours=5),
    )

    assert changed_recovery.first_attempt_at == NOW + timedelta(hours=5)
    restarted = SpotifyPlannerJournal(SQLiteStore(store.path))
    assert restarted.get_persistence_rejection_barrier("source", "edition-one") == barrier
    assert barrier.protects(store.get_match_state(*edition.identity))

    assert store.set_match_state(
        *edition.identity,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:durable-b",
        spotify_duration_seconds=61,
        diagnostics="accepted B",
        updated_at=NOW + timedelta(hours=6),
    )
    assert not barrier.protects(store.get_match_state(*edition.identity))
    restarted.clear_persistence_rejection_barrier("source", "edition-one")
    assert restarted.get_persistence_rejection_barrier("source", "edition-one") is None


def test_schema_six_upgrade_seeds_implicit_persistence_rejection_barrier(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    edition = _edition()
    store.upsert_editions((edition,), observed_at=NOW)
    assert store.set_match_state(
        *edition.identity,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:durable-a",
        spotify_duration_seconds=60,
        diagnostics="durable A",
        updated_at=NOW + timedelta(hours=4),
    )
    journal = SpotifyPlannerJournal(store)
    journal.record_matched_recovery(
        source_id="source",
        source_native_id="edition-one",
        input_fingerprint=SHA_A,
        attempted_at=NOW,
    )

    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE spotify_planner_persistence_rejection_state")
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")

    legacy = SQLiteStore(store.path)
    assert legacy.schema_version() == 6
    legacy.initialize()

    restarted = SpotifyPlannerJournal(SQLiteStore(store.path))
    barrier = restarted.get_persistence_rejection_barrier("source", "edition-one")
    assert barrier is not None
    durable = store.get_match_state(*edition.identity)
    assert durable is not None
    assert barrier.protects(durable)
    assert barrier.first_rejected_at == NOW
    assert barrier.last_rejected_at == NOW
