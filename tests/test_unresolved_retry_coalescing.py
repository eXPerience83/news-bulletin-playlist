from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from test_engine import (
    NOW,
    _Auth,
    _config,
    _EmptyCatalogueSpotify,
    _rss,
    _rss_two,
    _source,
    _Spotify,
)

from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import CanonicalEdition
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.spotify.client import SpotifyTransportError
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


def _integrated_runner(
    tmp_path: Path,
    *,
    spotify: _Spotify,
    current: list[datetime],
    fetcher: Callable[[str], bytes],
) -> tuple[EngineRunner, _Auth, list[str]]:
    store = SQLiteStore(tmp_path / "coalescing-integration.sqlite3")
    store.initialize()
    auth = _Auth()
    client_factory_calls: list[str] = []

    def client_factory(token: str) -> _Spotify:
        client_factory_calls.append(token)
        return spotify

    runner = EngineRunner(
        _config("first"),
        store,
        auth,
        fetcher=fetcher,
        client_factory=client_factory,
        clock=lambda: current[0],
    )
    return runner, auth, client_factory_calls


def _edition(native_id: str, *, minutes_old: int = 1) -> CanonicalEdition:
    observed = NOW - timedelta(minutes=minutes_old)
    return CanonicalEdition(
        source_id=_source().id,
        source_native_id=native_id,
        title=f"unresolved {native_id}",
        published_at=observed,
        edition_at=observed,
    )


def _stored_edition(runner: EngineRunner, native_id: str) -> CanonicalEdition:
    return next(
        edition
        for edition in runner.store.list_editions(source_id=_source().id)
        if edition.source_native_id == native_id
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
    _persist_unresolved(runner, second_trigger, attempted_at=NOW - timedelta(minutes=20))
    _evaluate(runner, (second_trigger, non_due), now=second_now)
    second = runner.planner_journal.get_unresolved(str(non_due.source_id), non_due.source_native_id)

    assert second is not None
    assert second.first_unresolved_at == before.first_unresolved_at
    assert second.last_attempt_at == second_now
    assert second.next_retry_at == second_now + unresolved_retry_interval(
        second_now - before.first_unresolved_at
    )
    assert second.next_retry_at > first.next_retry_at


def test_run_cycle_non_due_unresolved_alone_stays_provider_free(tmp_path: Path) -> None:
    current = [NOW]
    spotify = _EmptyCatalogueSpotify()
    runner, auth, client_factory_calls = _integrated_runner(
        tmp_path,
        spotify=spotify,
        current=current,
        fetcher=lambda _url: _rss(),
    )
    assert runner.run_cycle().ok
    before = runner.planner_journal.get_unresolved("ser", "ser-1")
    assert before is not None
    provider_counts = (auth.calls, len(client_factory_calls), spotify.show_calls)
    current[0] = NOW + timedelta(minutes=5)

    result = runner.run_cycle()

    after = runner.planner_journal.get_unresolved("ser", "ser-1")
    diagnostic = result.sources[0].matching_diagnostics
    assert result.ok
    assert result.provider_session_created is False
    assert (auth.calls, len(client_factory_calls), spotify.show_calls) == provider_counts
    assert diagnostic.matching_work_count == 0
    assert diagnostic.matching_reason_retry_due_count == 0
    assert diagnostic.matching_reason_coalesced_unresolved_count == 0
    assert diagnostic.catalogue_requested is False
    assert diagnostic.catalogue_request_count == 0
    assert after == before


def test_run_cycle_due_retry_coalesces_non_due_with_one_catalogue(tmp_path: Path) -> None:
    current = [NOW]
    spotify = _EmptyCatalogueSpotify()
    runner, _auth, _client_factory_calls = _integrated_runner(
        tmp_path,
        spotify=spotify,
        current=current,
        fetcher=lambda _url: _rss_two(),
    )
    assert runner.run_cycle().ok
    non_due = _stored_edition(runner, "ser-2")
    before = _persist_unresolved(
        runner,
        non_due,
        attempted_at=NOW + timedelta(minutes=10),
    )
    show_calls = spotify.show_calls
    current[0] = NOW + timedelta(minutes=15)

    result = runner.run_cycle()

    after = runner.planner_journal.get_unresolved("ser", "ser-2")
    diagnostic = result.sources[0].matching_diagnostics
    assert result.ok
    assert diagnostic.matching_work_count == 2
    assert diagnostic.matching_reason_retry_due_count == 1
    assert diagnostic.matching_reason_coalesced_unresolved_count == 1
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is True
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == show_calls + 1
    assert after is not None
    assert after.first_unresolved_at == before.first_unresolved_at
    assert after.last_attempt_at == current[0]
    assert after.next_retry_at == current[0] + unresolved_retry_interval(
        current[0] - before.first_unresolved_at
    )


def test_run_cycle_catalogue_audit_coalesces_non_due_with_no_extra_catalogue(
    tmp_path: Path,
) -> None:
    current = [NOW]
    spotify = _EmptyCatalogueSpotify()
    runner, _auth, _client_factory_calls = _integrated_runner(
        tmp_path,
        spotify=spotify,
        current=current,
        fetcher=lambda _url: _rss(),
    )
    assert runner.run_cycle().ok
    before = runner.planner_journal.get_unresolved("ser", "ser-1")
    assert before is not None
    context = catalogue_context_fingerprint(_source(), _AUTH_CONTEXT)
    runner.planner_journal.verify_catalogue(
        context,
        context,
        verified_at=NOW - timedelta(hours=6),
    )
    show_calls = spotify.show_calls
    current[0] = NOW + timedelta(minutes=5)

    result = runner.run_cycle()

    after = runner.planner_journal.get_unresolved("ser", "ser-1")
    diagnostic = result.sources[0].matching_diagnostics
    assert result.ok
    assert diagnostic.catalogue_audit_due is True
    assert diagnostic.matching_work_count == 1
    assert diagnostic.matching_reason_retry_due_count == 0
    assert diagnostic.matching_reason_coalesced_unresolved_count == 1
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is True
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == show_calls + 1
    assert after is not None
    assert after.first_unresolved_at == before.first_unresolved_at
    assert after.last_attempt_at == current[0]


def test_run_cycle_catalogue_failure_does_not_record_coalesced_attempt(
    tmp_path: Path,
) -> None:
    class ToggleFailingCatalogueSpotify(_EmptyCatalogueSpotify):
        fail = False

        def show_episodes(
            self,
            show_id: str,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> dict[str, Any]:
            if not self.fail:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            self.show_calls += 1
            raise SpotifyTransportError("catalogue failure sentinel")

    current = [NOW]
    spotify = ToggleFailingCatalogueSpotify()
    runner, _auth, _client_factory_calls = _integrated_runner(
        tmp_path,
        spotify=spotify,
        current=current,
        fetcher=lambda _url: _rss_two(),
    )
    assert runner.run_cycle().ok
    non_due = _stored_edition(runner, "ser-2")
    before = _persist_unresolved(
        runner,
        non_due,
        attempted_at=NOW + timedelta(minutes=10),
    )
    spotify.fail = True
    show_calls = spotify.show_calls
    current[0] = NOW + timedelta(minutes=15)

    result = runner.run_cycle()

    after = runner.planner_journal.get_unresolved("ser", "ser-2")
    diagnostic = result.sources[0].matching_diagnostics
    assert result.sources[0].matching_ok is False
    assert diagnostic.matching_work_count == 2
    assert diagnostic.matching_reason_retry_due_count == 1
    assert diagnostic.matching_reason_coalesced_unresolved_count == 1
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is False
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == show_calls + 1
    assert after == before


def test_run_cycle_cooldown_blocks_due_and_coalesced_work_without_attempts(
    tmp_path: Path,
) -> None:
    current = [NOW]
    spotify = _EmptyCatalogueSpotify()
    runner, auth, client_factory_calls = _integrated_runner(
        tmp_path,
        spotify=spotify,
        current=current,
        fetcher=lambda _url: _rss_two(),
    )
    assert runner.run_cycle().ok
    non_due = _stored_edition(runner, "ser-2")
    before = _persist_unresolved(
        runner,
        non_due,
        attempted_at=NOW + timedelta(minutes=10),
    )
    current[0] = NOW + timedelta(minutes=15)
    runner.rate_limit_journal.activate(observed_at=current[0], retry_after_seconds=600)
    provider_counts = (auth.calls, len(client_factory_calls), spotify.show_calls)

    result = runner.run_cycle()

    after = runner.planner_journal.get_unresolved("ser", "ser-2")
    diagnostic = result.sources[0].matching_diagnostics
    assert result.provider_session_created is False
    assert diagnostic.matching_work_count == 2
    assert diagnostic.matching_reason_retry_due_count == 1
    assert diagnostic.matching_reason_coalesced_unresolved_count == 1
    assert diagnostic.catalogue_requested is False
    assert diagnostic.catalogue_inspected is False
    assert diagnostic.catalogue_request_count == 0
    assert (auth.calls, len(client_factory_calls), spotify.show_calls) == provider_counts
    assert after == before
