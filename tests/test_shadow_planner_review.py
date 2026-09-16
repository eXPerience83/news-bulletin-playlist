from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest
from test_engine import NOW, SER_URI, _Auth, _config, _rss, _source, _Spotify
from test_spotify_auth import _authorize, _service

from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.persistence import MatchStatus, PersistenceError, SQLiteStore
from news_bulletin_playlist.reconciliation import MutationOutcome
from news_bulletin_playlist.snapshot_pending import PendingSnapshotJournal, _ConnectionContext
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError
from news_bulletin_playlist.spotify.matcher import match_source_editions
from news_bulletin_playlist.spotify_planner import (
    DestinationVerificationContext,
    PlannerReason,
    SpotifyPlannerJournal,
    authorization_context_fingerprint,
    catalogue_context_fingerprint,
    edition_input_fingerprint,
    matching_input_fingerprint,
    unresolved_planning_reason,
)


class SpySpotify(_Spotify):
    def __init__(self):
        super().__init__()
        self.trace = []

    def show_episodes(self, *args, **kwargs):
        self.trace.append(("catalogue", args, kwargs))
        return super().show_episodes(*args, **kwargs)

    def playlist_items(self, *args, **kwargs):
        self.trace.append(("items", args, kwargs))
        return super().playlist_items(*args, **kwargs)

    def playlist_snapshot(self, *args, **kwargs):
        self.trace.append(("snapshot", args, kwargs))
        return super().playlist_snapshot(*args, **kwargs)

    def replace_playlist_items(self, *args, **kwargs):
        self.trace.append(("replace", args, kwargs))
        return super().replace_playlist_items(*args, **kwargs)


def setup(tmp_path, *, spotify=None, auth=None, playlists=("first",)):
    tmp_path.mkdir(exist_ok=True)
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    spotify = spotify if spotify is not None else SpySpotify()
    auth = auth if auth is not None else _Auth()
    factories = []

    def factory(token):
        factories.append(True)
        return spotify

    runner = EngineRunner(
        _config(*playlists),
        store,
        auth,
        fetcher=lambda _: _rss(),
        client_factory=factory,
        clock=lambda: NOW,
    )
    return runner, spotify, auth, factories


def assert_unsafe(result):
    assert result.planner_shadow is not None
    assert not result.planner_shadow.state_available
    assert result.planner_shadow.would_need_spotify
    assert not result.planner_shadow.would_allow_local_skip


@pytest.mark.parametrize("stage", ["body", "commit", "rollback", "close"])
def test_pending_context_translates_sqlite_and_closes(stage):
    connection = Mock()
    original = sqlite3.OperationalError("sentinel")
    if stage != "body":
        getattr(connection, stage).side_effect = original
    with (
        pytest.raises(PersistenceError) as error,
        _ConnectionContext(connection, operation="pending", path="private"),
    ):
        if stage == "body":
            raise original
        if stage == "rollback":
            raise sqlite3.IntegrityError("original body")
    assert isinstance(error.value.__cause__, sqlite3.Error)
    connection.close.assert_called_once()
    assert "sentinel" not in str(error.value)


def test_pending_preserves_non_sqlite_body_even_if_cleanup_fails():
    connection = Mock()
    connection.rollback.side_effect = sqlite3.OperationalError("cleanup")
    original = LookupError("application error")
    with (
        pytest.raises(LookupError) as error,
        _ConnectionContext(connection, operation="pending", path="private"),
    ):
        raise original
    assert error.value is original
    connection.close.assert_called_once()


@pytest.mark.parametrize("operation", ["get", "set", "clear"])
def test_pending_operations_never_leak_sqlite(tmp_path, monkeypatch, operation):
    runner, _, _, _ = setup(tmp_path)
    journal = PendingSnapshotJournal(runner.store)
    connection = Mock()
    connection.execute.side_effect = sqlite3.OperationalError("SQL sentinel")
    monkeypatch.setattr(
        journal,
        "_connection",
        lambda _: _ConnectionContext(connection, operation=operation, path="private"),
    )
    with pytest.raises(PersistenceError):
        getattr(journal, operation)(Mock())


@pytest.mark.parametrize("failed_get", [1, 3])
def test_pending_shadow_failure_preserves_provider_trace(tmp_path, monkeypatch, failed_get):
    baseline, provider, auth, factories = setup(tmp_path / "baseline")
    assert baseline.run_cycle().ok
    expected = provider.trace
    runner, provider, auth, factories = setup(tmp_path / "failure")
    real_get = PendingSnapshotJournal.get
    calls = 0

    def get(self, playlist_id):
        nonlocal calls
        calls += 1
        if calls == failed_get:
            connection = Mock()
            with _ConnectionContext(connection, operation="get", path="private"):
                raise sqlite3.OperationalError("injected SELECT failure")
        return real_get(self, playlist_id)

    monkeypatch.setattr(PendingSnapshotJournal, "get", get)
    result = runner.run_cycle()
    assert result.ok
    assert auth.calls == len(factories) == 1
    assert provider.trace == expected
    assert_unsafe(result)


@pytest.mark.parametrize(
    "method",
    [
        "get_catalogue",
        "observe_destination",
        "begin_destination_verification",
        "observe_match_input",
        "verify_catalogue",
        "verify_destination",
        "retain_contexts",
    ],
)
@pytest.mark.parametrize("error", [PersistenceError, sqlite3.OperationalError])
def test_all_shadow_boundaries_preserve_actual_calls(tmp_path, monkeypatch, method, error):
    baseline, provider, _, _ = setup(tmp_path / "baseline", playlists=("first", "second"))
    baseline.run_cycle()
    expected = provider.trace
    runner, provider, auth, factories = setup(tmp_path / "failure", playlists=("first", "second"))
    runner.local_skips_enabled = False
    monkeypatch.setattr(runner.planner_journal, method, Mock(side_effect=error("injected")))
    result = runner.run_cycle()
    if method in {"observe_destination", "begin_destination_verification"}:
        assert not result.ok
        assert auth.calls == len(factories) == 1
        assert [entry[0] for entry in provider.trace] == ["catalogue"]
        assert result.planner_shadow is not None
        assert result.planner_shadow.would_need_spotify
        assert not result.planner_shadow.would_allow_local_skip
        if method == "observe_destination":
            assert not result.planner_shadow.state_available
        return
    assert result.ok
    assert auth.calls == len(factories) == 1
    assert provider.trace == expected
    assert_unsafe(result)


@pytest.mark.parametrize("error", [PersistenceError, sqlite3.OperationalError])
def test_failed_semantic_disqualification_is_shadow_only(tmp_path, monkeypatch, error):
    runner, provider, auth, factories = setup(tmp_path)
    assert runner.run_cycle().ok
    first_trace = tuple(provider.trace)
    original = provider.show_episodes

    def contradicted(*args, **kwargs):
        page = original(*args, **kwargs)
        replacement = {**page["items"][0], "uri": "spotify:episode:" + "2" * 22}
        page["items"][0]["name"] = "unrelated"
        page["items"].append(replacement)
        page["total"] = len(page["items"])
        return page

    provider.show_episodes = contradicted
    runner.clock = lambda: NOW + timedelta(hours=6)
    monkeypatch.setattr(
        runner.planner_journal,
        "clear_match_input",
        Mock(side_effect=error("injected")),
    )
    result = runner.run_cycle()
    assert result.ok
    assert auth.calls == len(factories) == 2
    assert tuple(provider.trace[: len(first_trace)]) == first_trace
    assert len(provider.trace) > len(first_trace)
    assert_unsafe(result)


@pytest.mark.parametrize(
    "field",
    [
        "desired_fingerprint",
        "destination_context_fingerprint",
        "config_fingerprint",
        "auth_context_fingerprint",
    ],
)
def test_verification_requires_exact_operation_context_after_restart(tmp_path, field):
    runner, _, _, _ = setup(tmp_path)
    runner.run_cycle()
    journal = runner.planner_journal
    prior = journal.get_destination("first")
    journal.begin_destination_verification("first")
    expected = DestinationVerificationContext(
        prior.observed_desired_fingerprint,
        prior.destination_context_fingerprint,
        prior.config_fingerprint,
        prior.auth_context_fingerprint,
    )
    expected = replace(expected, **{field: "b" * 64})
    with pytest.raises(PersistenceError):
        journal.verify_destination("first", expected=expected, verified_at=NOW)
    restarted = SpotifyPlannerJournal(runner.store).get_destination("first")
    assert restarted.dirty and restarted.confirmed_desired_fingerprint is None


def test_failed_observation_never_certifies_old_a_after_writing_b(tmp_path, monkeypatch):
    runner, provider, _, _ = setup(tmp_path)
    runner.local_skips_enabled = False
    runner.run_cycle()
    old_show = provider.show_episodes

    def removed(*args, **kwargs):
        page = old_show(*args, **kwargs)
        page["items"][0].update(uri="spotify:episode:" + "2" * 22, name="unrelated")
        return page

    monkeypatch.setattr(provider, "show_episodes", removed)
    runner.clock = lambda: NOW + timedelta(hours=6)
    with monkeypatch.context() as scope:
        scope.setattr(
            runner.planner_journal,
            "observe_destination",
            Mock(side_effect=PersistenceError("transient")),
        )
        result = runner.run_cycle()
    assert not result.ok
    assert provider.items["destination-first"] == [SER_URI]
    assert_unsafe(result)
    journal = SpotifyPlannerJournal(runner.store)
    state = journal.get_destination("first")
    assert not state.dirty and state.confirmed_desired_fingerprint is not None
    runner.store.set_match_state(
        _source().id,
        "ser-1",
        status=MatchStatus.MATCHED,
        updated_at=NOW,
        spotify_episode_uri=SER_URI,
        spotify_duration_seconds=60,
    )
    assert not runner._compute_shadow_plan(NOW).would_allow_local_skip


@pytest.mark.parametrize("failure", ["502", "transport"])
@pytest.mark.parametrize("bracket", ["stable", "divergent", "snapshot_failure"])
def test_ambiguous_result_and_qualified_evidence_are_independent(
    tmp_path, monkeypatch, failure, bracket
):
    class Ambiguous(SpySpotify):
        after_write = False
        reads = 0

        def replace_playlist_items(self, playlist_id, uris):
            super().replace_playlist_items(playlist_id, uris)
            self.after_write = True
            if failure == "502":
                raise SpotifyApiError(502, "safe sentinel")
            raise SpotifyTransportError("safe sentinel")

        def playlist_items(self, playlist_id, **kwargs):
            if self.after_write:
                self.reads += 1
                if self.reads == 2 and bracket == "divergent":
                    self.items[playlist_id] = ["spotify:episode:" + "3" * 22]
                    self.snapshots[playlist_id] = "concurrent"
            return super().playlist_items(playlist_id, **kwargs)

        def playlist_snapshot(self, playlist_id):
            if self.after_write and bracket == "snapshot_failure":
                raise SpotifyTransportError("snapshot failure")
            return super().playlist_snapshot(playlist_id)

    runner, _, _, _ = setup(tmp_path, spotify=Ambiguous())
    captured = []
    import news_bulletin_playlist.engine as engine

    original = engine.reconcile_spotify_playlist

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(kwargs["evidence"])
        return result

    monkeypatch.setattr(engine, "reconcile_spotify_playlist", observe)
    result = runner.run_cycle()
    assert result.ok
    assert captured[0].mutation_outcome is MutationOutcome.VERIFIED
    assert captured[0].remote_evidence_qualified == (bracket == "stable")
    state = runner.planner_journal.get_destination("first")
    assert (state.confirmed_desired_fingerprint is not None) == (bracket == "stable")
    assert (
        runner.store.get_playlist_attestation(_config("first").playlists[0].id) is not None
    ) == (bracket == "stable")


def test_normal_write_and_unchanged_read_publish_structured_evidence(tmp_path, monkeypatch):
    runner, _, _, _ = setup(tmp_path)
    runner.local_skips_enabled = False
    captured = []
    import news_bulletin_playlist.engine as engine

    original = engine.reconcile_spotify_playlist

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(result)
        return result

    monkeypatch.setattr(engine, "reconcile_spotify_playlist", observe)
    assert runner.run_cycle().ok
    assert runner.run_cycle().ok
    assert captured[0].mutation_outcome is MutationOutcome.VERIFIED
    assert captured[0].remote_evidence_qualified
    assert captured[1].mutation_outcome is MutationOutcome.NOT_ATTEMPTED
    assert captured[1].remote_evidence_qualified


@pytest.mark.parametrize("phase", ["prewrite", "400", "502", "readback", "snapshot", "success"])
def test_mutation_outcome_without_error_string_parsing(tmp_path, monkeypatch, phase):
    class Failing(SpySpotify):
        written = False

        def replace_playlist_items(self, playlist_id, uris):
            if phase in {"400", "502"}:
                raise SpotifyApiError(int(phase), "safe")
            response = super().replace_playlist_items(playlist_id, uris)
            self.written = True
            return response

        def playlist_items(self, playlist_id, **kwargs):
            if phase == "prewrite" or (self.written and phase == "readback"):
                raise SpotifyTransportError("opaque error")
            return super().playlist_items(playlist_id, **kwargs)

        def playlist_snapshot(self, playlist_id):
            if self.written and phase == "snapshot":
                raise SpotifyTransportError("opaque error")
            return super().playlist_snapshot(playlist_id)

    runner, _, _, _ = setup(tmp_path, spotify=Failing())
    result = runner.run_cycle()
    state = runner.planner_journal.get_destination("first")
    assert state.unknown == (phase in {"502", "readback", "snapshot"})
    assert result.ok == (phase == "success")
    expected_write_outcome = (
        "unknown"
        if phase in {"502", "readback", "snapshot"}
        else "verified"
        if phase == "success"
        else None
    )
    assert result.playlists[0].write_outcome == expected_write_outcome


@pytest.mark.parametrize("change", ["same", "time", "ambiguous", "different"])
def test_cached_input_requires_pure_semantic_qualification(tmp_path, change):
    runner, provider, _, _ = setup(tmp_path)
    runner.run_cycle()
    edition = runner.store.list_editions(source_id=_source().id)[0]
    if change == "time":
        edition = replace(edition, edition_at=edition.edition_at - timedelta(hours=1))
    old_show = provider.show_episodes

    def page(*args, **kwargs):
        value = old_show(*args, **kwargs)
        if change in {"ambiguous", "different"}:
            value["items"].append({**value["items"][0], "uri": "spotify:episode:" + "2" * 22})
            value["total"] = len(value["items"])
        if change == "different":
            value["items"][0]["name"] = "unrelated"
        return value

    provider.show_episodes = page
    before = runner.store.get_match_state(edition.source_id, edition.source_native_id)
    calls = provider.show_calls
    result = match_source_editions(provider, runner.store, _source(), [edition], now=NOW)
    outcome = result.outcomes[0]
    expected_qualification = {
        "same": True,
        "time": False,
        "ambiguous": None,
        "different": False,
    }[change]
    assert outcome.from_cache and outcome.spotify_episode_uri == SER_URI
    assert outcome.planner_input_evidence_qualified is expected_qualification
    assert provider.show_calls == calls + 1
    assert runner.store.get_match_state(edition.source_id, edition.source_native_id) == before
    runner._observe_matching_result(_source(), result, NOW)
    persisted = runner.planner_journal.get_match_input(
        str(edition.source_id), edition.source_native_id
    )
    if change in {"same", "ambiguous"}:
        assert persisted is not None
        assert persisted.input_fingerprint == matching_input_fingerprint(_source(), edition)
    else:
        assert persisted is None or persisted.input_fingerprint != matching_input_fingerprint(
            _source(), edition
        )


def test_authorization_context_refresh_reconnect_market(tmp_path):
    service, transport, _ = _service(tmp_path)
    assert service.planner_authorization_context() is None
    _authorize(service)
    initial = service.planner_authorization_context()
    service.get_access_token(now=NOW + timedelta(hours=2))
    assert transport.refresh_calls
    assert service.planner_authorization_context() == initial
    url = service.start_authorization(now=NOW + timedelta(hours=3))
    from urllib.parse import parse_qs, urlencode, urlsplit

    state = parse_qs(urlsplit(url).query)["state"][0]
    service.complete_callback(
        urlencode({"state": state, "code": "fake-code"}), now=NOW + timedelta(hours=3)
    )
    changed = service.planner_authorization_context()
    assert changed != initial
    assert catalogue_context_fingerprint(_source(), initial) != catalogue_context_fingerprint(
        _source(), changed
    )
    assert authorization_context_fingerprint(NOW, market="ES") != authorization_context_fingerprint(
        NOW, market="PL"
    )
    assert authorization_context_fingerprint(NOW) != authorization_context_fingerprint(
        NOW, market="ES"
    )


@pytest.mark.parametrize(
    "assignment",
    [
        "dirty=0, confirmed_desired_fingerprint=NULL",
        "dirty=0, confirmed_desired_fingerprint='" + "b" * 64 + "'",
        "dirty=0, unknown=1",
        "unknown=1",
        "dirty=1",
        "next_audit_at='2027-08-30T16:00:00.000000Z'",
    ],
)
def test_semantically_corrupt_destination_fails_closed_after_restart(tmp_path, assignment):
    runner, _, _, _ = setup(tmp_path)
    runner.run_cycle()
    with sqlite3.connect(runner.store.path) as connection:
        connection.execute("UPDATE spotify_planner_destination_state SET " + assignment)
    with pytest.raises(PersistenceError):
        SpotifyPlannerJournal(runner.store).get_destination("first")
    assert not runner._compute_shadow_plan(NOW).would_allow_local_skip


@pytest.mark.parametrize(
    "table,column",
    [
        ("spotify_planner_catalogue_state", "next_audit_at"),
        ("spotify_planner_unresolved_state", "next_retry_at"),
        ("spotify_planner_unresolved_state", "first_unresolved_at"),
    ],
)
def test_corrupt_deadlines_are_not_skip_permission(tmp_path, table, column):
    runner, _, _, _ = setup(tmp_path)
    runner.run_cycle()
    journal = runner.planner_journal
    edition = runner.store.list_editions(source_id=_source().id)[0]
    journal.record_unresolved(
        source_id=str(edition.source_id),
        source_native_id=edition.source_native_id,
        input_fingerprint=edition_input_fingerprint(edition),
        status=MatchStatus.PENDING,
        attempted_at=NOW,
    )
    with sqlite3.connect(runner.store.path) as connection:
        connection.execute(f"UPDATE {table} SET {column} = ?", ("2027-08-30T10:00:00.000000Z",))
    restarted = SpotifyPlannerJournal(runner.store)
    with pytest.raises(PersistenceError):
        if table.endswith("catalogue_state"):
            restarted.get_catalogue(catalogue_context_fingerprint(_source(), "a" * 64))
        else:
            restarted.get_unresolved(str(edition.source_id), edition.source_native_id)


def test_missing_retry_is_not_input_changed_across_restart(tmp_path):
    runner, _, _, _ = setup(tmp_path)
    runner.run_cycle()
    edition = runner.store.list_editions(source_id=_source().id)[0]
    runner.store.set_match_state(
        edition.source_id, edition.source_native_id, status=MatchStatus.PENDING, updated_at=NOW
    )
    state = SpotifyPlannerJournal(runner.store).get_unresolved(
        str(edition.source_id), edition.source_native_id
    )
    assert state is None
    assert (
        unresolved_planning_reason(
            state, input_fingerprint=edition_input_fingerprint(edition), now=NOW
        )
        is PlannerReason.MISSING_EVIDENCE
    )
    assert PlannerReason.MISSING_EVIDENCE in runner._compute_shadow_plan(NOW).reasons
