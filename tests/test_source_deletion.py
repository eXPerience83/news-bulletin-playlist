"""Source-local deletion must never turn historical evidence into runtime authority."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist import engine_runtime
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.engine_cycle_synchronization import EngineCycleSynchronization
from news_bulletin_playlist.engine_runtime import (
    ConfigurationSynchronization,
    ReloadingEngineCycleRunner,
)
from news_bulletin_playlist.managed_provisioning import ProvisioningIntent, ProvisioningState
from news_bulletin_playlist.managed_state import (
    ManagedSource,
    ManagedState,
    ManagedStateStore,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.source_admin import (
    EngineCycleBusyError,
    SourceAdminService,
    SourceMutationError,
    SourceReferencedError,
)
from news_bulletin_playlist.spotify.first_models import (
    RefreshReason,
    SpotifyFirstEpisode,
    TimestampSource,
)
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetectorJournal,
    PublicEpisodeObservation,
)
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
EPISODE = "E" * 22


class _NeverAuth:
    def __init__(self) -> None:
        self.calls = 0

    def get_access_token(self, *, now: datetime | None = None) -> str:
        self.calls += 1
        raise AssertionError("source deletion must not acquire a Spotify token")


def _setup(
    tmp_path: Path,
) -> tuple[
    SourceAdminService, SpotifyFirstJournal, PublicDetectorJournal, EngineCycleSynchronization
]:
    db = SQLiteStore(tmp_path / "state.sqlite3")
    db.initialize()
    first = SpotifyFirstJournal(db)
    first.initialize()
    detector = PublicDetectorJournal(db)
    detector.initialize()
    gate = EngineCycleSynchronization()
    service = SourceAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        ConfigurationSynchronization(),
        _NeverAuth(),
        SpotifyRateLimitJournal(db),
        None,
        first_journal=first,
        detector_journal=detector,
        cycle_synchronization=gate,
    )
    return service, first, detector, gate


def _source(service: SourceAdminService, letter: str) -> ManagedSource:
    return service.validate_new_source(
        show_id=letter * 22,
        display_name=f"Source {letter}",
        origin_kind="country",
        origin_code="ES",
        language="es",
        editorial_scope="NAT",
        timezone="Europe/Madrid",
        parser_id="release_date_title",
        collection_filter_id=None,
    )


def _seed(
    service: SourceAdminService,
    first: SpotifyFirstJournal,
    detector: PublicDetectorJournal,
    source: ManagedSource,
) -> None:
    config = compile_engine_config(BUILTIN_CATALOG, ManagedState(sources=(source,)))
    definition = next(item for item in config.sources if item.id == source.id)
    episode = SpotifyFirstEpisode(
        source_id=source.id,
        spotify_show_id=source.spotify_show_id,
        spotify_episode_id=EPISODE,
        spotify_episode_uri=f"spotify:episode:{EPISODE}",
        title="Accepted bulletin",
        edition_at=None,
        timestamp_source=TimestampSource.FIRST_SEEN,
        release_date_raw=NOW.date().isoformat(),
        release_date_precision="day",
        duration_seconds=305,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    first.replace_source_episodes(source.id, (episode,), coverage_proven=True)
    first.record_refresh(
        definition,
        refreshed_at=NOW,
        reason=RefreshReason.BOOTSTRAP,
        detector_episode_id=EPISODE,
        api_episode_id=EPISODE,
        request_count=1,
        complete=True,
        coverage_proven=True,
        coverage_cutoff_at=NOW - timedelta(days=2),
    )
    detector.record_success(
        PublicEpisodeObservation(
            source_id=source.id,
            spotify_show_id=source.spotify_show_id,
            episode_id=EPISODE,
            episode_uri=f"spotify:episode:{EPISODE}",
            name="Accepted bulletin",
            title="Accepted bulletin",
            release_date_raw=NOW.date().isoformat(),
            duration_ms=305_000,
            related_entity_uri=f"spotify:show:{source.spotify_show_id}",
            observed_at=NOW,
        )
    )


def _history(first: SpotifyFirstJournal, source_id: SourceId) -> tuple[SpotifyFirstEpisode, ...]:
    return first.list_episode_history(
        observed_since=NOW - timedelta(days=1),
        observed_until=NOW + timedelta(days=1),
        source_id=source_id,
    )


def test_delete_clears_only_operational_source_state_and_readd_needs_bootstrap(
    tmp_path: Path,
) -> None:
    service, first, detector, _ = _setup(tmp_path)
    target, unrelated = _source(service, "A"), _source(service, "B")
    playlist = activate_template(BUILTIN_CATALOG.playlist("spain_spanish_news"), "destination")
    service.store.save(ManagedState(sources=(target, unrelated), playlists=(playlist,)))
    _seed(service, first, detector, target)
    _seed(service, first, detector, unrelated)
    with sqlite3.connect(first.path) as connection:
        connection.execute(
            "INSERT INTO spotify_first_destination_state VALUES (?, ?, ?, ?)",
            (str(playlist.id), "destination", "fingerprint", NOW.isoformat()),
        )
    before_playlists = service.store.load().playlists
    assert service.delete(str(target.id)) is True
    state = service.store.load()
    assert state.sources == (unrelated,) and state.playlists == before_playlists
    assert first.list_episodes(source_ids=(target.id,)) == ()
    assert first.get_source_state(target.id) is None
    assert detector.get(target.id, target.spotify_show_id) is None
    assert len(_history(first, target.id)) == 1
    assert len(first.list_episodes(source_ids=(unrelated.id,))) == 1
    assert first.get_source_state(unrelated.id) is not None
    assert detector.get(unrelated.id, unrelated.spotify_show_id) is not None
    assert len(_history(first, unrelated.id)) == 1
    with sqlite3.connect(first.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM spotify_first_destination_state WHERE playlist_id = ?",
            (str(playlist.id),),
        ).fetchone() == (1,)
    assert isinstance(service.auth, _NeverAuth) and service.auth.calls == 0

    # Re-add produces the same deterministic ID, but retained research history cannot authorize
    # playlist readiness or repopulate the current episode/source tables.
    readded = _source(service, "A")
    assert readded.id == target.id
    assigned = replace(playlist, source_ids=(target.id,))
    service.store.save(ManagedState(sources=(unrelated, readded), playlists=(assigned,)))
    config = compile_engine_config(BUILTIN_CATALOG, service.store.load())
    runner = object.__new__(EngineRunner)
    runner.config = config
    runner.first_journal = first
    assert not runner._playlist_sources_ready(config.playlists[0], now=NOW)
    assert first.list_episodes(source_ids=(target.id,)) == ()
    assert len(_history(first, target.id)) == 1


def test_delete_references_include_paused_playlists_and_pending_activation(tmp_path: Path) -> None:
    service, first, detector, _ = _setup(tmp_path)
    target = _source(service, "A")
    playlist = replace(
        activate_template(BUILTIN_CATALOG.playlist("spain_spanish_news"), "destination"),
        enabled=False,
        source_ids=(target.id,),
    )
    service.store.save(ManagedState(sources=(target,), playlists=(playlist,)))
    _seed(service, first, detector, target)
    with pytest.raises(SourceReferencedError):
        service.delete(str(target.id))
    assert first.get_source_state(target.id) is not None

    service.store.save(ManagedState(sources=(target,)))
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    intent = ProvisioningIntent(
        state=ProvisioningState.REQUEST_STARTED,
        template_id=template.id,
        display_name="Pending",
        description="",
        cover_id=template.cover_id,
        source_ids=(target.id,),
        retention_hours=template.retention_hours,
        max_episodes=template.max_episodes,
        max_duration_seconds=template.duration_policy.default_max_seconds,
    )
    service.provisioning_journal.save(intent)
    with pytest.raises(SourceReferencedError):
        service.delete(str(target.id))
    assert first.get_source_state(target.id) is not None
    service.provisioning_journal.save(replace(intent, source_ids=(SourceId("ser"),)))
    assert service.delete(str(target.id)) is False


def test_delete_rejects_builtins_and_missing_source_without_cleanup(tmp_path: Path) -> None:
    service, first, detector, _ = _setup(tmp_path)
    target = _source(service, "A")
    service.store.save(ManagedState(sources=(target,)))
    _seed(service, first, detector, target)
    for source_id in ("ser", "custom_" + "Z" * 22):
        with pytest.raises(SourceMutationError):
            service.delete(source_id)
    assert service.store.load().sources == (target,)
    assert first.get_source_state(target.id) is not None


def test_clear_operations_are_idempotent_and_source_local(tmp_path: Path) -> None:
    service, first, detector, _ = _setup(tmp_path)
    target, unrelated = _source(service, "A"), _source(service, "B")
    _seed(service, first, detector, target)
    _seed(service, first, detector, unrelated)
    for _ in range(2):
        first.clear_source_runtime_state(target.id)
        detector.clear_source_state(target.id)
    assert first.list_episodes(source_ids=(target.id,)) == ()
    assert first.get_source_state(target.id) is None
    assert detector.get(target.id, target.spotify_show_id) is None
    assert len(_history(first, target.id)) == 1
    assert first.get_source_state(unrelated.id) is not None
    assert detector.get(unrelated.id, unrelated.spotify_show_id) is not None


def test_multi_playlist_reference_blocks_until_last_explicit_unassignment(tmp_path: Path) -> None:
    service, _, _, _ = _setup(tmp_path)
    target = _source(service, "A")
    first = replace(
        activate_template(BUILTIN_CATALOG.playlist("spain_spanish_news"), "destination-A"),
        source_ids=(target.id,),
    )
    second = replace(
        activate_template(BUILTIN_CATALOG.playlist("international_english_news"), "destination-B"),
        source_ids=(SourceId("cnn"), target.id),
    )
    service.store.save(ManagedState(sources=(target,), playlists=(first, second)))
    with pytest.raises(SourceReferencedError):
        service.delete(str(target.id))
    service.store.save(
        ManagedState(sources=(target,), playlists=(replace(first, source_ids=("ser",)), second))
    )
    with pytest.raises(SourceReferencedError):
        service.delete(str(target.id))
    final = replace(second, source_ids=(SourceId("cnn"),))
    service.store.save(
        ManagedState(sources=(target,), playlists=(replace(first, source_ids=("ser",)), final))
    )
    assert service.delete(str(target.id)) is True
    assert service.store.load().playlists == (replace(first, source_ids=("ser",)), final)


def test_final_locked_reference_recheck_rejects_stale_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, first, detector, gate = _setup(tmp_path)
    target = _source(service, "A")
    service.store.save(ManagedState(sources=(target,)))
    _seed(service, first, detector, target)
    original_hold = gate.try_hold_maintenance

    @contextmanager
    def add_reference_before_final_check() -> Any:
        with original_hold() as acquired:
            playlist = replace(
                activate_template(BUILTIN_CATALOG.playlist("spain_spanish_news"), "destination"),
                source_ids=(target.id,),
            )
            service.store.save(ManagedState(sources=(target,), playlists=(playlist,)))
            yield acquired

    monkeypatch.setattr(gate, "try_hold_maintenance", add_reference_before_final_check)
    with pytest.raises(SourceReferencedError):
        service.delete(str(target.id))
    assert service.store.load().sources == (target,)
    assert first.get_source_state(target.id) is not None
    assert detector.get(target.id, target.spotify_show_id) is not None


@pytest.mark.parametrize("failure", ["first", "detector", "save"])
def test_partial_delete_failure_keeps_definition_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    service, first, detector, _ = _setup(tmp_path)
    target = _source(service, "A")
    service.store.save(ManagedState(sources=(target,)))
    _seed(service, first, detector, target)
    owner: Any = {"first": first, "detector": detector, "save": service.store}[failure]
    method = {
        "first": "clear_source_runtime_state",
        "detector": "clear_source_state",
        "save": "save",
    }[failure]
    original = getattr(owner, method)

    def fail(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise PersistenceError("secret-sqlite-path")

    monkeypatch.setattr(owner, method, fail)
    with pytest.raises(PersistenceError):
        service.delete(str(target.id))
    assert service.store.load().sources == (target,)
    assert len(_history(first, target.id)) == 1
    assert (first.get_source_state(target.id) is not None) == (failure == "first")
    assert (detector.get(target.id, target.spotify_show_id) is not None) == (
        failure in {"first", "detector"}
    )
    monkeypatch.setattr(owner, method, original)
    assert service.delete(str(target.id)) is False
    assert service.store.load().sources == ()
    assert first.list_episodes(source_ids=(target.id,)) == ()
    assert detector.get(target.id, target.spotify_show_id) is None


def test_cycle_gate_covers_complete_runner_and_busy_delete_is_non_destructive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, first, detector, gate = _setup(tmp_path)
    target = _source(service, "A")
    service.store.save(ManagedState(sources=(target,)))
    _seed(service, first, detector, target)
    runner = object.__new__(ReloadingEngineCycleRunner)
    runner.cycle_synchronization = gate
    entered, release = threading.Event(), threading.Event()

    def blocked_cycle() -> Any:
        entered.set()
        assert release.wait(timeout=5)
        return None

    monkeypatch.setattr(runner, "_run_cycle", blocked_cycle)
    thread = threading.Thread(target=runner.run_cycle)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(EngineCycleBusyError):
            service.delete(str(target.id))
        assert service.store.load().sources == (target,)
        assert first.get_source_state(target.id) is not None
        assert detector.get(target.id, target.spotify_show_id) is not None
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert service.delete(str(target.id)) is False


def test_cycle_and_delete_acquire_gate_before_configuration_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, first, detector, gate = _setup(tmp_path)
    target = _source(service, "A")
    service.store.save(ManagedState(sources=(target,)))
    _seed(service, first, detector, target)

    config_lock = service.synchronization
    observed: list[str] = []

    class OrderedConfiguration:
        @contextmanager
        def hold(self) -> Any:
            if len(observed) != 1:  # The first Delete hold is a read-only cheap precheck.
                with gate.try_hold_maintenance() as acquired:
                    assert not acquired, "destructive configuration lock preceded the cycle gate"
            observed.append("configuration")
            with config_lock.hold():
                yield

    runner = object.__new__(ReloadingEngineCycleRunner)
    runner.cycle_synchronization = gate
    runner.configuration_synchronization = OrderedConfiguration()
    runner.data_dir = tmp_path
    runner.environ = {}
    monkeypatch.setattr(engine_runtime, "_load_runtime_config", lambda *_: None)
    assert runner.run_cycle().ok is False
    service.synchronization = OrderedConfiguration()
    assert service.delete(str(target.id)) is False
    assert observed == ["configuration", "configuration", "configuration"]


def test_custom_draft_reference_blocks_source_deletion_even_when_hidden(tmp_path: Path) -> None:
    service, _, _, _ = _setup(tmp_path)
    source = _source(service, "Q")
    draft = LocalPlaylistTemplate(
        id=PlaylistId("custom_hidden_edition"),
        display_name="Saved draft",
        description="An installation-owned draft that still uses a custom source",
        default_source_ids=(source.id,),
        cover_id="spain_spanish_news",
        hidden=True,
    )
    service.store.save(ManagedState(schema_version=7, sources=(source,), templates=(draft,)))

    _, used_by, pending = service.deletion_references(str(source.id))
    assert used_by == ("Saved draft (custom template)",)
    assert pending is False
    with pytest.raises(SourceReferencedError, match="Unassign"):
        service.delete(str(source.id))
    assert service.store.load().sources == (source,)
    assert service.store.load().templates == (draft,)
