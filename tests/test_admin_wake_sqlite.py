from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from _http_handler import HandlerCaptureMixin, captured_response, initialize_handler_capture
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.diagnostics_runtime import DiagnosticOperationalHealthHandler
from news_bulletin_playlist.engine import EngineCycleResult, EngineScheduler
from news_bulletin_playlist.engine_cycle_synchronization import EngineCycleSynchronization
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.engine_runtime import (
    ConfigurationSynchronization,
    EngineLifecycleController,
    ReloadingEngineCycleRunner,
)
from news_bulletin_playlist.lan_admin import LanAdminSecurity
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_state import (
    ManagedSource,
    ManagedState,
    ManagedStateStore,
    activate_template,
)
from news_bulletin_playlist.models import (
    CountryCode,
    EditorialScope,
    LanguageTag,
    ParserId,
    SourceId,
    SourceOrigin,
)
from news_bulletin_playlist.persistence import DEFAULT_DB_FILENAME, SQLiteStore
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.source_admin import SourceAdminService
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetector,
    PublicDetectorJournal,
    PublicEpisodeObservation,
)
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal
from news_bulletin_playlist.spotify_metrics import (
    SpotifyPublicOperation,
    SpotifyRequestMetrics,
    SpotifyRequestOutcome,
    SpotifyWebApiOperation,
)
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store

PASSWORD = "long-enough-admin-password"
SHOW = "A" * 22
CUSTOM_ID = SourceId(f"custom_{SHOW}")


class _AdminHandler(HandlerCaptureMixin, DiagnosticOperationalHealthHandler):
    pass


class _Providers:
    """Controlled provider substitutes; every attempt must belong to the engine thread."""

    def __init__(self, metrics: SpotifyRequestMetrics, progress: threading.Event) -> None:
        self.metrics = metrics
        self.progress = progress
        self.calls: list[str] = []
        self.uris: tuple[str, ...] = ()
        self.snapshot = 0
        self.block_next = False
        self.release = threading.Event()

    def _attempt(self, operation: str) -> None:
        assert threading.current_thread().name == "news-playlist-engine"
        self.calls.append(operation)
        outcome = SpotifyRequestOutcome.SUCCESS
        if operation in {"embed_show", "oembed"}:
            self.metrics.record_public(SpotifyPublicOperation(operation), outcome)
        elif operation != "auth":
            self.metrics.record_web_api(SpotifyWebApiOperation(operation), outcome)

    def get_access_token(self, *, now: datetime | None = None) -> str:
        self._attempt("auth")
        return "test-token"

    def authorization_context(self) -> str:
        return "test-grant"

    def observe_episode(
        self, source_id: SourceId, spotify_show_id: str, *, observed_at: datetime
    ) -> PublicEpisodeObservation:
        if self.block_next:
            self.block_next = False
            self.progress.set()
            assert self.release.wait(10), "provider continuation was not released"
        self._attempt("embed_show")
        episode_id = ("C" if source_id == CUSTOM_ID else "B") * 22
        return PublicEpisodeObservation(
            source_id=source_id,
            spotify_show_id=spotify_show_id,
            episode_id=episode_id,
            episode_uri=f"spotify:episode:{episode_id}",
            name="Bulletin",
            title="Bulletin",
            release_date_raw=observed_at.date().isoformat(),
            duration_ms=300_000,
            related_entity_uri=f"spotify:show:{spotify_show_id}",
            observed_at=observed_at,
        )

    def oembed_title(self, show_id: str) -> str:
        self._attempt("oembed")
        return "Bulletin"

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self._attempt("show_episodes")
        episode_id = ("C" if show_id == SHOW else "B") * 22
        return catalogue_page(
            [
                {
                    "id": episode_id,
                    "uri": f"spotify:episode:{episode_id}",
                    "name": "Bulletin",
                    "release_date": datetime.now(UTC).date().isoformat(),
                    "release_date_precision": "day",
                    "duration_ms": 300_000,
                }
            ],
            offset=offset,
            limit=limit,
        )

    def playlist_items(
        self, destination_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        self._attempt("playlist_items")
        return {
            "items": [{"item": {"type": "episode", "uri": uri}} for uri in self.uris],
            "next": None,
            "total": len(self.uris),
        }

    def playlist_snapshot(self, destination_id: str) -> dict[str, Any]:
        self._attempt("playlist_snapshot")
        return {"snapshot_id": f"snapshot-{self.snapshot}"}

    def replace_playlist_items(self, destination_id: str, uris: list[str]) -> dict[str, Any]:
        self._attempt("replace_playlist_items")
        self.uris = tuple(uris)
        self.snapshot += 1
        return {"snapshot_id": f"snapshot-{self.snapshot}"}


class _CycleRecorder:
    def __init__(self, runner: InstrumentedEngineCycleRunner, progress: threading.Event) -> None:
        self.runner = runner
        self.progress = progress
        self.results: list[EngineCycleResult] = []
        self.errors: list[Exception] = []
        self.condition = threading.Condition()
        self.active = 0
        self.max_active = 0

    def run_cycle(self) -> EngineCycleResult:
        with self.condition:
            self.active += 1
            self.max_active = max(self.active, self.max_active)
        try:
            result = self.runner.run_cycle()
        except Exception as exc:
            with self.condition:
                self.errors.append(exc)
                self.progress.set()
            raise
        else:
            with self.condition:
                self.results.append(result)
            return result
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def wait_cycles(self, count: int) -> None:
        with self.condition:
            assert self.condition.wait_for(
                lambda: len(self.results) + len(self.errors) >= count and self.active == 0,
                timeout=10,
            ), "scheduler did not finish the expected cycles"


class _InitializationRace:
    """Hold the real schema snapshot and the competing lifecycle event transaction.

    With deferred BEGIN, let the event commit before the initializer's DELETE. With
    a reserved writer, the event's INSERT waits in SQLite until initialization commits.
    No SQLite result, error, busy timeout or provider outcome is simulated here.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, competing_event: str) -> None:
        self.armed = False
        self.initializing = False
        self.immediate = False
        self.schema_read = threading.Event()
        self.writer_attempted = threading.Event()
        self.writer_held = threading.Event()
        self.release_writer = threading.Event()
        self.writer_committed = threading.Event()
        self.wake_committed = threading.Event()
        self.failure: tuple[str, sqlite3.Error] | None = None
        connect = sqlite3.connect
        initialize = DiagnosticEventStore.initialize
        race = self

        def initialize_with_hook(store: DiagnosticEventStore) -> None:
            if race.armed and threading.current_thread().name == "news-playlist-engine":
                if competing_event != "scheduler_wake_requested":
                    # The handler emits the configuration event after lifecycle.reconcile.
                    # Let its preceding wake event finish before reserving the initializer.
                    assert race.wake_committed.wait(10)
                race.initializing = True
                try:
                    initialize(store)
                finally:
                    race.initializing = False
                    race.armed = False
            else:
                initialize(store)

        class Connection(sqlite3.Connection):
            competing_writer = False
            wake_writer = False

            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                initializing = (
                    race.initializing and threading.current_thread().name == "news-playlist-engine"
                )
                if initializing and sql == "BEGIN IMMEDIATE":
                    race.immediate = True
                competing = (
                    race.armed
                    and "INSERT INTO diagnostic_events" in sql
                    and parameters[3] == competing_event
                )
                if race.armed and "INSERT INTO diagnostic_events" in sql:
                    self.wake_writer = parameters[3] == "scheduler_wake_requested"
                if competing:
                    assert race.schema_read.wait(10)
                    self.competing_writer = True
                    race.writer_attempted.set()
                if initializing and "WHERE event_name = 'duration_profile'" in sql:
                    assert race.writer_attempted.wait(10)
                    if not race.immediate:
                        assert race.writer_held.wait(10)
                    race.release_writer.set()
                    if not race.immediate:
                        assert race.writer_committed.wait(10)
                try:
                    cursor = super().execute(sql, parameters)
                except sqlite3.Error as exc:
                    if initializing:
                        race.failure = (sql, exc)
                    raise
                if initializing and "CREATE INDEX IF NOT EXISTS diagnostic_events_playlist" in sql:
                    race.schema_read.set()
                if competing:
                    race.writer_held.set()
                    assert race.release_writer.wait(10)
                return cursor

            def commit(self) -> None:
                super().commit()
                if self.competing_writer:
                    race.writer_committed.set()
                if self.wake_writer:
                    race.wake_committed.set()

        def connect_with_hook(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return connect(*args, **kwargs, factory=Connection)

        monkeypatch.setattr(sqlite3, "connect", connect_with_hook)
        monkeypatch.setattr(DiagnosticEventStore, "initialize", initialize_with_hook)


@pytest.mark.parametrize("mutation", ("enable", "disable", "assignment"))
@pytest.mark.parametrize("competing_writer", ("wake", "admin"))
def test_admin_mutation_wake_uses_new_durable_state_without_sqlite_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str, competing_writer: str
) -> None:
    _exercise_admin_wake(tmp_path, monkeypatch, mutation, competing_writer=competing_writer)


def test_real_sqlite_cleanup_failure_still_fails_cycle_before_provider_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exercise_admin_wake(tmp_path, monkeypatch, "enable", cleanup_failure=True)


def _exercise_admin_wake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    *,
    competing_writer: str = "wake",
    cleanup_failure: bool = False,
) -> None:
    store = SQLiteStore(tmp_path / DEFAULT_DB_FILENAME)
    store.initialize()
    event_store = DiagnosticEventStore(store.path)
    event_store.initialize()
    diagnostics = OperationalDiagnostics(event_store)
    metrics = SpotifyRequestMetrics(store.path)
    metrics.initialize()
    detector_journal = PublicDetectorJournal(store)
    detector_journal.initialize()
    first_journal = SpotifyFirstJournal(store)
    first_journal.initialize()
    rate_limit = SpotifyRateLimitJournal(store)
    configuration = ConfigurationSynchronization()
    cycle_gate = EngineCycleSynchronization()
    managed_store = ManagedStateStore(tmp_path / "managed-state.json")
    custom = ManagedSource(
        id=CUSTOM_ID,
        display_name="Test news",
        origin=SourceOrigin.country(CountryCode("ES")),
        language=LanguageTag("es"),
        editorial_scope=EditorialScope.NATIONAL,
        timezone="Europe/Madrid",
        spotify_show_id=SHOW,
        parser_id=ParserId("release_date_title"),
        collection_filter_id=None,
        enabled=mutation != "enable",
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    playlist = activate_template(template, "destination")
    playlist = replace(
        playlist,
        source_ids=(SourceId("abc"),) if mutation == "assignment" else (SourceId("abc"), CUSTOM_ID),
    )
    managed_store.save(ManagedState(sources=(custom,), playlists=(playlist,)))
    managed_store = open_managed_state_store(tmp_path)
    progress = threading.Event()
    providers = _Providers(metrics, progress)
    detector = PublicDetector(detector_journal, diagnostics, client=providers)
    runner = ReloadingEngineCycleRunner(
        tmp_path,
        {},
        store,
        providers,
        detector,
        detector_journal,
        first_journal,
        configuration,
        client_factory=lambda _token: providers,
        request_metrics=metrics,
        cycle_synchronization=cycle_gate,
    )
    observed = InstrumentedEngineCycleRunner(runner, diagnostics, request_metrics=metrics)
    recorder = _CycleRecorder(observed, progress)
    lifecycle = EngineLifecycleController(
        recorder, interval=timedelta(hours=1), diagnostics=diagnostics
    )

    # Keep the real Event-based scheduler waits for the Admin wake and queued wake.
    # Only the subsequent scheduled deadline is controlled instead of waiting an hour.
    original_wait = EngineScheduler._wait_for_wake_or_stop
    scheduled_wait = threading.Event()
    scheduled_deadline = threading.Event()
    second_wait_started = threading.Event()
    waits = 0

    def wait(scheduler: EngineScheduler, stop: threading.Event) -> None:
        nonlocal waits
        waits += 1
        if waits == 2:
            second_wait_started.set()
        if waits == 3:
            scheduled_wait.set()
            assert scheduled_deadline.wait(10)
        else:
            original_wait(scheduler, stop)

    monkeypatch.setattr(EngineScheduler, "_wait_for_wake_or_stop", wait)
    admin_event = (
        "admin_playlist_updated"
        if mutation == "assignment"
        else f"admin_source_{'enabled' if mutation == 'enable' else 'disabled'}"
    )
    race = _InitializationRace(
        monkeypatch,
        "scheduler_wake_requested" if competing_writer == "wake" else admin_event,
    )
    handler = _AdminHandler.__new__(_AdminHandler)
    handler.data_dir = tmp_path
    handler.admin_security = security = LanAdminSecurity(PASSWORD)
    handler.spotify_auth = None
    handler.operational_status = lifecycle.status
    handler.engine_lifecycle = lifecycle
    handler.configuration_synchronization = configuration
    handler.managed_admin_service = ManagedAdminService(
        managed_store, client_factory=lambda _: providers
    )
    handler.source_admin_service = SourceAdminService(
        managed_store,
        configuration,
        providers,
        rate_limit,
        metrics,
        client_factory=lambda _: providers,
        first_journal=first_journal,
        detector_journal=detector_journal,
        cycle_synchronization=cycle_gate,
    )
    handler.operational_diagnostics = diagnostics
    handler.diagnostic_store = event_store
    handler.request_metrics = metrics
    handler.managed_admin_auth = providers
    handler.auth_synchronization = None
    form = {"csrf_token": [security.issue_csrf_token()]}
    if mutation == "assignment":
        path = "/admin/playlists/update"
        form.update(
            playlist_id=[str(playlist.id)],
            display_name=[playlist.display_name],
            description=[playlist.description],
            cover_id=[playlist.cover_id],
            source_id=["abc", str(CUSTOM_ID)],
            enabled=["1"],
            max_duration_minutes=["30"],
            max_duration_seconds_remainder=["0"],
        )
    else:
        path = "/admin/sources/set-enabled"
        form.update(source_id=[str(CUSTOM_ID)], enabled=["1" if mutation == "enable" else "0"])
    initialize_handler_capture(handler, path=path, form=form, password=PASSWORD)

    try:
        lifecycle.reconcile(configured=True)
        recorder.wait_cycles(1)
        assert recorder.results[0].ok
        provider_count = len(providers.calls)
        original_uris = providers.uris
        if cleanup_failure:
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    "INSERT INTO diagnostic_events(occurred_at, severity, component, "
                    "event_name, details_json) VALUES (?, 'INFO', 'playlist.eligibility', "
                    "'duration_profile', '{}')",
                    (datetime.now(UTC).isoformat(),),
                )
                connection.execute(
                    "CREATE TRIGGER reject_legacy_cleanup BEFORE DELETE ON diagnostic_events "
                    "WHEN OLD.event_name = 'duration_profile' AND OLD.details_json = '{}' "
                    "BEGIN SELECT RAISE(ABORT, 'controlled cleanup rejection'); END"
                )
        race.armed = True
        providers.block_next = True
        handler.do_POST()
        assert captured_response(handler).status == HTTPStatus.SEE_OTHER
        assert progress.wait(10), "woken cycle reached neither provider work nor failure"
        durable = ManagedStateStore(managed_store.path).load()
        assert durable.sources[0].enabled is (mutation != "disable")
        assert durable.playlists[0].source_ids == (SourceId("abc"), CUSTOM_ID)
        assert len(providers.calls) == provider_count
        assert race.writer_committed.is_set()
        if cleanup_failure:
            recorder.wait_cycles(2)
            assert len(recorder.errors) == 1
            assert isinstance(recorder.errors[0], sqlite3.IntegrityError)
            assert recorder.errors[0].sqlite_errorname == "SQLITE_CONSTRAINT_TRIGGER"
            assert providers.uris == original_uris
            assert len(recorder.results) == 1
            events = event_store.list_events()
            failures = [event for event in events if event.event_name == "cycle_unexpected_failure"]
            assert len(failures) == 1
            assert failures[0].details["exception_class"] == "sqlite_error"
            accounting = [
                event
                for event in events
                if event.event_name == "cycle_request_accounting"
                and event.cycle_id == failures[0].cycle_id
            ]
            assert len(accounting) == 1
            assert accounting[0].details["total"] == 0
            assert second_wait_started.wait(10)
            last_cycle = lifecycle.status.snapshot().last_cycle
            assert last_cycle is not None and not last_cycle.ok
            return
        assert recorder.errors == [], (
            None
            if race.failure is None
            else (
                race.failure[0],
                type(race.failure[1]).__name__,
                str(race.failure[1]),
                race.failure[1].sqlite_errorcode,
                race.failure[1].sqlite_errorname,
            )
        )
        # The Admin request returned while engine provider work is still blocked.

        # A wake arriving during the cycle is retained and cannot overlap that cycle.
        lifecycle.wake()
        assert recorder.active == 1
        assert len(recorder.results) == 1
        providers.release.set()
        recorder.wait_cycles(3)
        assert scheduled_wait.wait(10)
        scheduled_deadline.set()
        recorder.wait_cycles(4)
        assert recorder.errors == []
        assert recorder.max_active == 1
        expected = {SourceId("abc")} if mutation == "disable" else {SourceId("abc"), CUSTOM_ID}
        for result in recorder.results[1:]:
            assert result.ok
            assert {source.source_id for source in result.sources} == expected
            assert result.playlists[0].desired_count == len(expected)
        assert len(providers.uris) == len(expected)
        assert ManagedStateStore(managed_store.path).load() == durable
        events = event_store.list_events()
        assert not any(event.event_name == "cycle_unexpected_failure" for event in events)
        assert sum(event.event_name == "cycle_completed" for event in events) == 4
        assert sum(event.event_name == "scheduler_wake_requested" for event in events) == 2
        assert sum(event.event_name == admin_event for event in events) == 1
        accounting = [event for event in events if event.event_name == "cycle_request_accounting"]
        assert len(accounting) == 4
        snapshot = metrics.last_24_hours()
        assert (
            sum(event.details["total"] for event in accounting)
            == snapshot.public_requests + snapshot.web_api_requests
        )
        assert snapshot.public_requests == sum(
            call in {"embed_show", "oembed"} for call in providers.calls
        )
        assert snapshot.web_api_requests == sum(
            call not in {"embed_show", "oembed", "auth"} for call in providers.calls
        )
        assert snapshot.oauth_requests == 0
        assert not any(
            call in {"change_playlist_details", "upload_playlist_cover"} for call in providers.calls
        )
    finally:
        providers.release.set()
        race.release_writer.set()
        scheduled_deadline.set()
        lifecycle.shutdown()
