from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from test_engine import NOW, _Auth, _config, _source, _Spotify

from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import CanonicalEdition
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.spotify.matcher import SpotifyCatalogueResult, match_source_editions
from news_bulletin_playlist.spotify_planner import (
    catalogue_context_fingerprint,
    matching_input_fingerprint,
    unresolved_retry_interval,
)

_AUTH_CONTEXT = "a" * 64


def _runner(tmp_path: Path) -> EngineRunner:
    store = SQLiteStore(tmp_path / "coalescing.sqlite3")
    store.initialize()
    runner = EngineRunner(
        _config("first"),
        store,
        _Auth(),
        fetcher=lambda _url: b"",
        client_factory=lambda _token: _Spotify(),
        clock=lambda: NOW,
    )
    runner._shadow_auth_context = _AUTH_CONTEXT
    source = _source()
    context = catalogue_context_fingerprint(source, _AUTH_CONTEXT)
    runner.planner_journal.verify_catalogue(context, context, verified_at=NOW)
    return runner


def _edition(native_id: str, *, minutes_old: int = 1) -> CanonicalEdition:
    observed = NOW - timedelta(minutes=minutes_old)
    return CanonicalEdition(
        source_id=_source().id,
        source_native_id=native_id,
        title=f"unresolved {native_id}",
        published_at=observed,
        edition_at=observed,
    )


def _persist_unresolved(
    runner: EngineRunner,
    edition: CanonicalEdition,
    *,
    attempted_at: datetime,
    status: MatchStatus = MatchStatus.PENDING,
):
    source = _source()
    runner.store.upsert_editions((edition,), observed_at=attempted_at)
    runner.store.set_match_state(
        edition.source_id,
        edition.source_native_id,
        status=status,
        updated_at=attempted_at,
    )
    fingerprint = matching_input_fingerprint(source, edition)
    runner.planner_journal.observe_match_input(
        source_id=str(edition.source_id),
        source_native_id=edition.source_native_id,
        input_fingerprint=fingerprint,
        observed_at=attempted_at,
    )
    return runner.planner_journal.record_unresolved(
        source_id=str(edition.source_id),
        source_native_id=edition.source_native_id,
        input_fingerprint=fingerprint,
        status=status,
        attempted_at=attempted_at,
    )


def _evaluate(
    runner: EngineRunner,
    editions: tuple[CanonicalEdition, ...],
    *,
    now: datetime,
) -> None:
    plan = runner._source_matching_plan(_source(), editions, now)
    result = match_source_editions(
        _Spotify(),
        runner.store,
        _source(),
        editions,
        now=now,
        catalogue=SpotifyCatalogueResult((), 1, True),
        evaluation_intents=plan.evaluation_intents,
        catalogue_request_count=1,
    )
    runner._observe_matching_result(_source(), result, now)


def test_due_retry_coalesces_non_due_unresolved_without_new_trigger(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    due = _edition("due", minutes_old=30)
    non_due = _edition("non-due", minutes_old=5)
    _persist_unresolved(runner, due, attempted_at=NOW - timedelta(minutes=20))
    _persist_unresolved(runner, non_due, attempted_at=NOW - timedelta(minutes=5))

    plan = runner._source_matching_plan(_source(), (due, non_due), NOW)

    assert set(plan.evaluation_intents) == {due.identity, non_due.identity}
    assert plan.needs_catalogue
    assert plan.diagnostics.matching_reason_retry_due_count == 1
    assert plan.diagnostics.matching_reason_coalesced_unresolved_count == 1
    assert plan.diagnostics.matching_work_count == 2


def test_non_due_unresolved_alone_does_not_authorize_catalogue(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    non_due = _edition("non-due")
    _persist_unresolved(runner, non_due, attempted_at=NOW - timedelta(minutes=5))

    plan = runner._source_matching_plan(_source(), (non_due,), NOW)

    assert plan.evaluation_intents == {}
    assert not plan.needs_catalogue
    assert plan.diagnostics.matching_reason_coalesced_unresolved_count == 0
    assert plan.diagnostics.matching_work_count == 0


def test_catalogue_audit_coalesces_non_due_unresolved(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    non_due = _edition("audit-coalesced")
    _persist_unresolved(runner, non_due, attempted_at=NOW - timedelta(minutes=5))
    source = _source()
    context = catalogue_context_fingerprint(source, _AUTH_CONTEXT)
    runner.planner_journal.verify_catalogue(
        context,
        context,
        verified_at=NOW - timedelta(hours=6),
    )

    plan = runner._source_matching_plan(source, (non_due,), NOW)

    assert plan.catalogue_audit_due
    assert set(plan.evaluation_intents) == {non_due.identity}
    assert plan.diagnostics.matching_reason_retry_due_count == 0
    assert plan.diagnostics.matching_reason_coalesced_unresolved_count == 1


def test_clock_rollback_unresolved_is_not_opportunistically_coalesced(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    future = _edition("future")
    _persist_unresolved(runner, future, attempted_at=NOW + timedelta(minutes=5))

    plan = runner._source_matching_plan(_source(), (future,), NOW)

    assert plan.evaluation_intents == {}
    assert not plan.needs_catalogue
    assert plan.diagnostics.matching_reason_coalesced_unresolved_count == 0


def test_coalesced_attempt_preserves_age_and_reschedules_from_actual_attempt(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    due = _edition("due", minutes_old=30)
    non_due = _edition("non-due", minutes_old=5)
    _persist_unresolved(runner, due, attempted_at=NOW - timedelta(minutes=20))
    before = _persist_unresolved(
        runner,
        non_due,
        attempted_at=NOW - timedelta(minutes=5),
    )

    _evaluate(runner, (due, non_due), now=NOW)
    after = runner.planner_journal.get_unresolved(str(non_due.source_id), non_due.source_native_id)

    assert after is not None
    assert after.first_unresolved_at == before.first_unresolved_at
    assert after.last_attempt_at == NOW
    assert after.next_retry_at == NOW + unresolved_retry_interval(NOW - before.first_unresolved_at)
    assert after.next_retry_at >= before.next_retry_at


def test_repeated_coalesced_attempts_align_deadline_to_latest_real_check(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    first_trigger = _edition("first-trigger", minutes_old=30)
    non_due = _edition("non-due", minutes_old=5)
    _persist_unresolved(runner, first_trigger, attempted_at=NOW - timedelta(minutes=20))
    before = _persist_unresolved(
        runner,
        non_due,
        attempted_at=NOW - timedelta(minutes=5),
    )

    _evaluate(runner, (first_trigger, non_due), now=NOW)
    first = runner.planner_journal.get_unresolved(str(non_due.source_id), non_due.source_native_id)
    assert first is not None

    second_now = NOW + timedelta(minutes=10)
    second_trigger = CanonicalEdition(
        source_id=_source().id,
        source_native_id="second-trigger",
        title="unresolved second-trigger",
        published_at=NOW - timedelta(minutes=30),
        edition_at=NOW - timedelta(minutes=30),
    )
    _evaluate(runner, (second_trigger, non_due), now=second_now)
    second = runner.planner_journal.get_unresolved(str(non_due.source_id), non_due.source_native_id)

    assert second is not None
    assert second.first_unresolved_at == before.first_unresolved_at
    assert second.last_attempt_at == second_now
    assert second.next_retry_at == second_now + unresolved_retry_interval(
        second_now - before.first_unresolved_at
    )
    assert second.next_retry_at > first.next_retry_at
