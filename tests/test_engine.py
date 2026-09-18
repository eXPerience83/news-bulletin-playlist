from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from _spotify_catalogue import catalogue_page

import news_bulletin_playlist.engine as engine_module
from news_bulletin_playlist.engine import (
    DestinationRuntimeLeaseRegistry,
    EngineCycleAlreadyRunning,
    EngineCycleResult,
    EngineRunner,
    EngineScheduler,
    OperationalStatus,
)
from news_bulletin_playlist.models import (
    AdapterId,
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
from news_bulletin_playlist.persistence import MatchStatus, PersistenceError, SQLiteStore
from news_bulletin_playlist.snapshot_pending import PendingSnapshotJournal
from news_bulletin_playlist.spotify.auth import SpotifyReauthorizationRequired
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError
from news_bulletin_playlist.spotify_backoff import ProviderAuthorizationSession
from news_bulletin_playlist.spotify_planner import DestinationVerificationContext

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
SER_URL = "https://example.test/ser.xml"
SER_TITLE = "Las noticias de la SER, 11:00 (30/08/2026)"
SER_URI = "spotify:episode:0000000000000000000001"


def test_destination_runtime_lease_uses_exact_context_and_monotonic_expiry() -> None:
    registry = DestinationRuntimeLeaseRegistry()
    playlist_id = PlaylistId("first")
    context_a = DestinationVerificationContext("a" * 64, "b" * 64, "c" * 64, "d" * 64)
    context_b = replace(context_a, desired_fingerprint="e" * 64)

    registry.publish(playlist_id, context_a, verified_monotonic=100.0)

    assert registry.allows(playlist_id, context_a, monotonic_now=100.0)
    assert registry.allows(playlist_id, context_a, monotonic_now=21_699.999)
    assert not registry.allows(playlist_id, context_a, monotonic_now=21_700.0)
    registry.publish(playlist_id, context_a, verified_monotonic=30_000.0)
    registry.revoke_unless(playlist_id, context_b)
    assert not registry.allows(playlist_id, context_a, monotonic_now=30_001.0)
    assert not registry.allows(playlist_id, context_b, monotonic_now=30_001.0)


class _Auth:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def planner_authorization_context(self) -> str:
        return "a" * 64

    def get_access_token(self, *, now: datetime | None = None) -> str:
        assert now is not None
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "access-token"


class _Spotify:
    def __init__(self, *, fail_playlist: str | None = None) -> None:
        self.fail_playlist = fail_playlist
        self.show_calls = 0
        self.playlist_reads: list[str] = []
        self.snapshot_read_calls: list[str] = []
        self.replacements: list[tuple[str, tuple[str, ...]]] = []
        self.items: dict[str, list[str]] = {}
        self.snapshots: dict[str, str] = {}

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "ser-show"
        assert limit == 50
        assert offset == 0
        self.show_calls += 1
        return catalogue_page(
            [
                {
                    "uri": SER_URI,
                    "name": SER_TITLE,
                    "release_date": "2026-08-30",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
            ],
            offset=offset,
            limit=limit,
        )

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert limit in {1, 50}
        self.playlist_reads.append(playlist_id)
        if playlist_id == self.fail_playlist:
            raise SpotifyTransportError("simulated playlist outage")
        uris = self.items.get(playlist_id, [])
        selected = uris[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in selected],
            "next": None,
            "total": len(uris),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        if playlist_id == self.fail_playlist:
            raise SpotifyTransportError("simulated playlist outage")
        self.items[playlist_id] = list(uris)
        self.replacements.append((playlist_id, tuple(uris)))
        snapshot = f"snapshot-{len(self.replacements)}"
        self.snapshots[playlist_id] = snapshot
        return {"snapshot_id": snapshot}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        self.snapshot_read_calls.append(playlist_id)
        return {"snapshot_id": self.snapshots.get(playlist_id, "snapshot-initial")}


def _source() -> SourceDefinition:
    return SourceDefinition(
        id=SourceId("ser"),
        display_name="SER",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ser"),
        endpoint_url=SER_URL,
        external_references=(
            ExternalReference(
                system="spotify",
                resource_type="show",
                external_id="ser-show",
            ),
        ),
    )


def _playlist(playlist_id: str) -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId(playlist_id),
        display_name=playlist_id,
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SourceId("ser"),)),
        destination=DestinationReference(
            adapter_id=AdapterId("spotify"),
            external_id=f"destination-{playlist_id}",
        ),
    )


def _config(*playlist_ids: str) -> EngineConfig:
    return EngineConfig(
        schema_version=1,
        sources=(_source(),),
        playlists=tuple(_playlist(value) for value in playlist_ids),
    )


def _rss() -> bytes:
    return (
        "<rss><channel><item>"
        "<guid>ser-1</guid>"
        f"<title>{SER_TITLE}</title>"
        "<pubDate>Sun, 30 Aug 2026 09:05:00 +0000</pubDate>"
        "</item></channel></rss>"
    ).encode()


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    return store


def test_cycle_fetches_shared_source_once_and_reconciles_two_playlists(tmp_path: Path) -> None:
    calls: list[str] = []
    spotify = _Spotify()

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return _rss()

    runner = EngineRunner(
        _config("first", "second"),
        _store(tmp_path),
        _Auth(),
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
        local_skips_enabled=False,
    )

    first = runner.run_cycle()
    second = runner.run_cycle()

    assert first.ok
    assert second.ok
    assert calls == [SER_URL, SER_URL]
    # The second stable cycle reuses durable match/audit evidence and does no catalogue I/O.
    assert spotify.show_calls == 1
    assert spotify.replacements == [
        ("destination-first", (SER_URI,)),
        ("destination-second", (SER_URI,)),
    ]
    assert all(outcome.desired_count == 1 for outcome in second.playlists)
    assert all(outcome.wrote is False for outcome in second.playlists)
    assert second.planner_shadow is not None
    assert second.planner_shadow.would_allow_local_skip
    with sqlite3.connect(runner.store.path) as connection:
        catalogue_states = connection.execute(
            "SELECT COUNT(*) FROM spotify_planner_catalogue_state"
        ).fetchone()[0]
    assert catalogue_states == 1


def test_matched_evidence_recovery_uses_durable_backoff_and_shared_catalogue(
    tmp_path: Path,
) -> None:
    class TruncatedSpotify(_Spotify):
        bounded = False

        def show_episodes(
            self,
            show_id: str,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> dict[str, Any]:
            if not self.bounded:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            assert limit == 50
            assert offset in {0, 50}
            self.show_calls += 1
            unrelated_uri = "spotify:episode:" + "9" * 22
            return catalogue_page(
                [
                    {
                        "uri": unrelated_uri,
                        "name": "Unrelated programme",
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    }
                ],
                offset=offset,
                limit=limit,
                next_url="more",
                total=101,
            )

    current = NOW
    old_title = SER_TITLE
    include_new = False
    spotify = TruncatedSpotify()
    auth = _Auth()

    def feed() -> bytes:
        items = [
            (
                "ser-1",
                old_title,
                "Sun, 30 Aug 2026 09:05:00 +0000",
            )
        ]
        if include_new:
            items.append(
                (
                    "ser-2",
                    "Las noticias de la SER, 12:00 (30/08/2026)",
                    "Sun, 30 Aug 2026 10:05:00 +0000",
                )
            )
        body = "".join(
            f"<item><guid>{native}</guid><title>{title}</title>"
            f"<pubDate>{published}</pubDate></item>"
            for native, title, published in items
        )
        return f"<rss><channel>{body}</channel></rss>".encode()

    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: feed(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    assert runner.run_cycle().ok
    assert spotify.show_calls == 1

    runner.planner_journal.clear_match_input("ser", "ser-1")
    spotify.bounded = True
    current = NOW + timedelta(minutes=10)

    first_recovery = runner.run_cycle()

    assert first_recovery.ok
    assert spotify.show_calls == 3
    recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert recovery is not None
    assert recovery.last_attempt_at == current
    assert recovery.next_retry_at == current + timedelta(hours=6)

    calls = spotify.show_calls
    auth_calls = auth.calls
    current = NOW + timedelta(minutes=20)
    skipped = runner.run_cycle()

    assert skipped.ok
    assert not skipped.provider_work_planned
    assert not skipped.provider_session_created
    assert spotify.show_calls == calls
    assert auth.calls == auth_calls

    old_title = "Las noticias de la SER, 11:01 (30/08/2026)"
    current = NOW + timedelta(minutes=30)
    changed = runner.run_cycle()

    assert changed.ok
    assert spotify.show_calls == calls + 2
    assert changed.sources[0].matching_diagnostics.matching_reason_input_changed_count == 1
    recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert recovery is not None
    assert recovery.next_retry_at == current + timedelta(hours=6)

    calls = spotify.show_calls
    current = NOW + timedelta(minutes=40)
    assert runner.run_cycle().ok
    assert spotify.show_calls == calls

    current = NOW + timedelta(hours=6, minutes=30)
    due = runner.run_cycle()
    assert due.ok
    assert spotify.show_calls == calls + 2
    assert due.sources[0].matching_diagnostics.matching_reason_evidence_recovery_count == 1

    calls = spotify.show_calls
    include_new = True
    current = NOW + timedelta(hours=6, minutes=40)
    new_match = runner.run_cycle()

    assert new_match.ok
    assert spotify.show_calls == calls + 2
    assert new_match.sources[0].matching_diagnostics.matching_reason_new_match_count == 1
    new_state = runner.store.get_match_state(SourceId("ser"), "ser-2")
    assert new_state is not None
    assert new_state.status is MatchStatus.PENDING


def test_rss_failure_with_lkg_does_not_force_spotify(tmp_path: Path) -> None:
    auth = _Auth()
    spotify = _Spotify()
    fail = False

    def fetcher(_url: str) -> bytes:
        if fail:
            raise OSError("feed unavailable")
        return _rss()

    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )
    assert runner.run_cycle().ok
    provider_counts = (auth.calls, spotify.show_calls, len(spotify.playlist_reads))
    fail = True

    degraded = runner.run_cycle()

    assert not degraded.ok
    assert not degraded.sources[0].collection_ok
    assert degraded.playlists[0].wrote is False
    assert (auth.calls, spotify.show_calls, len(spotify.playlist_reads)) == provider_counts


def test_dirty_destination_does_not_force_clean_sibling_remote_read(tmp_path: Path) -> None:
    auth = _Auth()
    spotify = _Spotify()
    runner = EngineRunner(
        _config("first", "second"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )
    assert runner.run_cycle().ok
    spotify.playlist_reads.clear()
    runner.planner_journal.begin_destination_verification("first")

    result = runner.run_cycle()

    assert result.ok
    assert set(spotify.playlist_reads) == {"destination-first"}
    assert result.destinations_remote == 1
    assert result.destinations_skipped == 1


def test_destination_audit_is_due_exactly_at_six_hours(tmp_path: Path) -> None:
    class SnapshotCountingSpotify(_Spotify):
        def __init__(self) -> None:
            super().__init__()
            self.snapshot_reads = 0

        def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
            self.snapshot_reads += 1
            return super().playlist_snapshot(playlist_id)

    auth = _Auth()
    spotify = SnapshotCountingSpotify()
    current = NOW
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    assert runner.run_cycle().ok
    item_reads = len(spotify.playlist_reads)
    snapshot_reads = spotify.snapshot_reads
    current = NOW + timedelta(hours=6) - timedelta(microseconds=1)
    assert runner.run_cycle().ok
    assert len(spotify.playlist_reads) == item_reads
    assert spotify.snapshot_reads == snapshot_reads
    current = NOW + timedelta(hours=6)

    due = runner.run_cycle()

    assert due.ok
    assert due.destinations_remote == 1
    assert len(spotify.playlist_reads) == item_reads
    assert spotify.snapshot_reads == snapshot_reads + 1


def test_wall_clock_rollback_cannot_extend_monotonic_destination_lease(tmp_path: Path) -> None:
    wall = [NOW]
    monotonic = [100.0]
    spotify = _Spotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: wall[0],
        monotonic_clock=lambda: monotonic[0],
    )
    assert runner.run_cycle().ok
    reads = len(spotify.playlist_reads)

    wall[0] = NOW + timedelta(hours=5)
    monotonic[0] += timedelta(hours=5).total_seconds()
    assert runner.run_cycle().ok
    assert len(spotify.playlist_reads) == reads

    wall[0] = NOW + timedelta(hours=4, minutes=30)
    monotonic[0] += timedelta(hours=1).total_seconds()
    due = runner.run_cycle()
    assert due.ok
    assert due.destinations_remote == 1
    assert len(spotify.playlist_reads) > reads


def test_unreadable_pending_snapshot_state_preserves_without_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = _Auth()
    spotify = _Spotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )
    assert runner.run_cycle().ok
    provider_counts = (auth.calls, spotify.show_calls, len(spotify.playlist_reads))
    monkeypatch.setattr(
        PendingSnapshotJournal,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PersistenceError("unreadable")),
    )

    blocked = runner.run_cycle()

    assert not blocked.ok
    assert blocked.playlists[0].wrote is None
    assert (auth.calls, spotify.show_calls, len(spotify.playlist_reads)) == provider_counts


@pytest.mark.parametrize(
    ("catalogue_kind", "remote_differs", "expected_ok"),
    [
        ("empty", False, True),
        ("truncated_missing", True, False),
        ("truncated_present", True, True),
    ],
)
def test_missing_match_input_evidence_preserves_valid_mapping(
    tmp_path: Path,
    catalogue_kind: str,
    remote_differs: bool,
    expected_ok: bool,
) -> None:
    class RecoverySpotify(_Spotify):
        kind = "normal"

        def show_episodes(
            self, show_id: str, *, limit: int = 50, offset: int = 0
        ) -> dict[str, Any]:
            if self.kind == "normal":
                return super().show_episodes(show_id, limit=limit, offset=offset)
            self.show_calls += 1
            if self.kind == "empty":
                return catalogue_page([], offset=offset, limit=limit)
            item = {
                "uri": (
                    SER_URI if self.kind == "truncated_present" else "spotify:episode:" + "2" * 22
                ),
                "name": SER_TITLE if self.kind == "truncated_present" else "unrelated",
                "release_date": "2026-08-30",
                "release_date_precision": "day",
                "duration_ms": 60_000,
            }
            return catalogue_page([item], offset=offset, limit=limit, next_url="more", total=101)

    spotify = RecoverySpotify()
    store = _store(tmp_path)
    first = EngineRunner(
        _config("first"),
        store,
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
        local_skips_enabled=False,
    )
    assert first.run_cycle().ok
    first.planner_journal.clear_match_input("ser", "ser-1")
    spotify.kind = catalogue_kind
    if remote_differs:
        spotify.items["destination-first"] = []
    writes_before = tuple(spotify.replacements)

    restarted = EngineRunner(
        _config("first"),
        SQLiteStore(store.path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW + timedelta(minutes=10),
        local_skips_enabled=False,
    )
    result = restarted.run_cycle()

    assert result.ok is expected_ok
    assert restarted.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    if catalogue_kind != "truncated_present":
        assert tuple(spotify.replacements) == writes_before
    after_restart = SQLiteStore(store.path)
    assert after_restart.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI


def test_corrupt_catalogue_evidence_preserves_match_and_blocks_unsafe_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TruncatedSpotify(_Spotify):
        truncated = False

        def show_episodes(
            self, show_id: str, *, limit: int = 50, offset: int = 0
        ) -> dict[str, Any]:
            if not self.truncated:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            self.show_calls += 1
            return catalogue_page(
                [
                    {
                        "uri": "spotify:episode:" + "2" * 22,
                        "name": "unrelated",
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    }
                ],
                offset=offset,
                limit=limit,
                next_url="more",
                total=101,
            )

    spotify = TruncatedSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
        local_skips_enabled=False,
    )
    assert runner.run_cycle().ok
    spotify.items["destination-first"] = []
    spotify.truncated = True

    def fail_catalogue(_context: str):
        raise PersistenceError("corrupt planner catalogue evidence")

    monkeypatch.setattr(runner.planner_journal, "get_catalogue", fail_catalogue)
    writes_before = tuple(spotify.replacements)
    result = runner.run_cycle()

    assert not result.ok
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    assert tuple(spotify.replacements) == writes_before


def test_source_contract_change_requires_semantic_requalification(
    tmp_path: Path,
) -> None:
    class DelayedSpotify(_Spotify):
        truncated = False

        def show_episodes(
            self, show_id: str, *, limit: int = 50, offset: int = 0
        ) -> dict[str, Any]:
            assert show_id == "ser-show"
            self.show_calls += 1
            item = {
                "uri": SER_URI,
                "name": SER_TITLE,
                "release_date": "2026-08-31",
                "release_date_precision": "day",
                "duration_ms": 60_000,
            }
            return catalogue_page(
                [item],
                offset=offset,
                limit=limit,
                next_url="more" if self.truncated else None,
                total=101 if self.truncated else 1,
            )

    original_source = replace(_source(), spotify_release_delay_days=1)
    changed_source = replace(original_source, spotify_release_delay_days=0)
    playlist = _playlist("first")
    spotify = DelayedSpotify()
    runner = EngineRunner(
        replace(_config("first"), sources=(original_source,)),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
        local_skips_enabled=False,
    )
    assert runner.run_cycle().ok
    spotify.truncated = True
    runner.config = replace(runner.config, sources=(changed_source,))

    assert runner.run_cycle().ok
    state = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert state is not None and state.status is MatchStatus.PENDING
    calls_after_requalification = spotify.show_calls
    assert runner.run_cycle().ok
    assert spotify.show_calls == calls_after_requalification
    assert runner.config.playlists == (playlist,)


def test_remote_confirmation_is_bound_to_reconciled_desired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spotify = _Spotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
        local_skips_enabled=False,
    )
    assert runner.run_cycle().ok
    original = engine_module.reconcile_spotify_playlist
    replacement_uri = "spotify:episode:" + "2" * 22

    def reconcile_then_change_local(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        runner.store.set_match_state(
            SourceId("ser"),
            "ser-1",
            status=MatchStatus.MATCHED,
            spotify_episode_uri=replacement_uri,
            spotify_duration_seconds=60,
            diagnostics="concurrent local change",
            updated_at=NOW,
        )
        return result

    monkeypatch.setattr(engine_module, "reconcile_spotify_playlist", reconcile_then_change_local)
    result = runner.run_cycle()
    state = runner.planner_journal.get_destination("first")

    assert result.ok
    assert spotify.items["destination-first"] == [SER_URI]
    assert state is not None and state.dirty
    assert state.confirmed_desired_fingerprint != state.observed_desired_fingerprint


def test_post_matching_desired_observation_failure_preserves_and_revokes_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spotify = _Spotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )
    assert runner.run_cycle().ok
    writes_before = tuple(spotify.replacements)
    durable_a = runner.planner_journal.get_destination("first")
    assert durable_a is not None
    context_a = DestinationVerificationContext(
        durable_a.observed_desired_fingerprint,
        durable_a.destination_context_fingerprint,
        durable_a.config_fingerprint,
        durable_a.auth_context_fingerprint,
    )
    assert runner.destination_leases.allows(
        PlaylistId("first"), context_a, monotonic_now=runner.monotonic_clock()
    )
    runner.planner_journal.clear_match_input("ser", "ser-1")
    replacement_uri = "spotify:episode:" + "2" * 22
    real_match = engine_module.match_source_editions

    def match_then_change(*args: Any, **kwargs: Any):
        result = real_match(*args, **kwargs)
        runner.store.set_match_state(
            SourceId("ser"),
            "ser-1",
            status=MatchStatus.MATCHED,
            spotify_episode_uri=replacement_uri,
            spotify_duration_seconds=60,
            diagnostics="matching changed desired",
            updated_at=NOW,
        )
        return result

    real_observe = runner.planner_journal.observe_destination

    def fail_only_for_b(**kwargs: Any):
        if kwargs["desired_fingerprint"] != context_a.desired_fingerprint:
            raise PersistenceError("simulated post-matching persistence failure")
        return real_observe(**kwargs)

    monkeypatch.setattr(engine_module, "match_source_editions", match_then_change)
    monkeypatch.setattr(runner.planner_journal, "observe_destination", fail_only_for_b)

    result = runner.run_cycle()

    assert not result.ok
    assert result.playlists[0].write_outcome == "not_attempted"
    assert tuple(spotify.replacements) == writes_before
    assert not runner.destination_leases.allows(
        PlaylistId("first"), context_a, monotonic_now=runner.monotonic_clock()
    )
    durable_after = runner.planner_journal.get_destination("first")
    assert durable_after is not None
    assert durable_after.confirmed_desired_fingerprint == context_a.desired_fingerprint


def test_provider_session_auth_change_replans_and_labels_evidence_with_session_context(
    tmp_path: Path,
) -> None:
    class ReconnectingAuth(_Auth):
        context = "a" * 64

        def planner_authorization_context(self) -> str:
            return self.context

        def acquire_provider_session(
            self, *, now: datetime | None = None
        ) -> ProviderAuthorizationSession:
            assert now is not None
            self.calls += 1
            self.context = "b" * 64
            return ProviderAuthorizationSession("access-token", self.context)

    auth = ReconnectingAuth()
    spotify = _Spotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()
    state = runner.planner_journal.get_destination("first")

    assert result.ok
    assert state is not None
    assert state.auth_context_fingerprint == "b" * 64
    assert state.confirmed_desired_fingerprint == state.observed_desired_fingerprint


def test_auth_change_after_session_acquisition_blocks_mutation_and_confirmation(
    tmp_path: Path,
) -> None:
    class ChangingAuth(_Auth):
        reads = 0

        def planner_authorization_context(self) -> str:
            self.reads += 1
            if self.reads >= 2:
                return "c" * 64
            return "b" * 64

        def acquire_provider_session(
            self, *, now: datetime | None = None
        ) -> ProviderAuthorizationSession:
            assert now is not None
            self.calls += 1
            return ProviderAuthorizationSession("access-token", "b" * 64)

    spotify = _Spotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        ChangingAuth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()
    state = runner.planner_journal.get_destination("first")

    assert not result.ok
    assert spotify.replacements == []
    assert state is not None
    assert state.confirmed_desired_fingerprint is None


def test_auth_change_after_preplan_preserves_instead_of_global_local_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ReconnectingAuth(_Auth):
        context = "a" * 64

        def planner_authorization_context(self) -> str:
            return self.context

    auth = ReconnectingAuth()
    spotify = _Spotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )
    assert runner.run_cycle().ok
    durable = runner.planner_journal.get_destination("first")
    assert durable is not None
    context_a = DestinationVerificationContext(
        durable.observed_desired_fingerprint,
        durable.destination_context_fingerprint,
        durable.config_fingerprint,
        durable.auth_context_fingerprint,
    )
    original_plan_destinations = runner._plan_destinations

    def reconnect_after_preplan(*args: Any, **kwargs: Any):
        result = original_plan_destinations(*args, **kwargs)
        auth.context = "b" * 64
        return result

    monkeypatch.setattr(runner, "_plan_destinations", reconnect_after_preplan)
    provider_counts = (auth.calls, spotify.show_calls, tuple(spotify.playlist_reads))

    result = runner.run_cycle()

    assert not result.ok
    assert result.provider_work_planned is False
    assert result.playlists[0].write_outcome == "not_attempted"
    assert (auth.calls, spotify.show_calls, tuple(spotify.playlist_reads)) == provider_counts
    assert not runner.destination_leases.allows(
        PlaylistId("first"), context_a, monotonic_now=runner.monotonic_clock()
    )


def test_auth_change_during_remote_sibling_preserves_clean_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MutableAuth(_Auth):
        context = "b" * 64

        def planner_authorization_context(self) -> str:
            return self.context

    auth = MutableAuth()
    spotify = _Spotify()
    runner = EngineRunner(
        _config("first", "second"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )
    assert runner.run_cycle().ok
    durable = runner.planner_journal.get_destination("second")
    assert durable is not None
    second_context = DestinationVerificationContext(
        durable.observed_desired_fingerprint,
        durable.destination_context_fingerprint,
        durable.config_fingerprint,
        durable.auth_context_fingerprint,
    )
    runner.planner_journal.begin_destination_verification("first")
    original_reconcile = engine_module.reconcile_spotify_playlist

    def reconnect_after_first(*args: Any, **kwargs: Any):
        result = original_reconcile(*args, **kwargs)
        if args[1].id == PlaylistId("first"):
            auth.context = "c" * 64
        return result

    monkeypatch.setattr(engine_module, "reconcile_spotify_playlist", reconnect_after_first)

    result = runner.run_cycle()
    outcomes = {outcome.playlist_id: outcome for outcome in result.playlists}

    assert outcomes[PlaylistId("second")].write_outcome == "not_attempted"
    assert outcomes[PlaylistId("second")].wrote is None
    assert not runner.destination_leases.allows(
        PlaylistId("second"), second_context, monotonic_now=runner.monotonic_clock()
    )
    assert all(destination != "destination-second" for destination, _ in spotify.replacements[2:])


def test_valid_truncated_audit_renews_without_proving_absence(tmp_path: Path) -> None:
    clock = [NOW]

    class TruncatedSpotify(_Spotify):
        truncated = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.truncated:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            self.show_calls += 1
            return catalogue_page(
                [
                    {
                        "uri": SER_URI,
                        "name": SER_TITLE,
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    }
                ],
                offset=offset,
                limit=limit,
                next_url="more",
                total=101,
            )

    spotify = TruncatedSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    spotify.truncated = True
    clock[0] = NOW + timedelta(hours=6)
    assert runner.run_cycle().ok
    calls_after_audit = spotify.show_calls
    clock[0] += timedelta(minutes=10)
    assert runner.run_cycle().ok

    assert calls_after_audit == 3
    assert spotify.show_calls == calls_after_audit
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI


def test_valid_empty_audit_renews_and_preserves_mapping(tmp_path: Path) -> None:
    clock = [NOW]

    class EmptyAuditSpotify(_Spotify):
        empty = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.empty:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            del show_id
            self.show_calls += 1
            return catalogue_page([], offset=offset, limit=limit)

    spotify = EmptyAuditSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    spotify.empty = True
    clock[0] = NOW + timedelta(hours=6)
    assert runner.run_cycle().ok
    calls_after_audit = spotify.show_calls
    clock[0] += timedelta(minutes=10)
    assert runner.run_cycle().ok

    assert spotify.show_calls == calls_after_audit == 2
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI


@pytest.mark.parametrize("truncated", [False, True])
def test_prewrite_absence_invalidates_only_with_complete_nonempty_catalogue(
    tmp_path: Path, truncated: bool
) -> None:
    class MissingSpotify(_Spotify):
        missing = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.missing:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            del show_id
            self.show_calls += 1
            return catalogue_page(
                [
                    {
                        "uri": "spotify:episode:" + "2" * 22,
                        "name": "unrelated",
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    }
                ],
                offset=offset,
                limit=limit,
                next_url="more" if truncated else None,
                total=101 if truncated else 1,
            )

    spotify = MissingSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
        local_skips_enabled=False,
    )
    assert runner.run_cycle().ok
    spotify.items["destination-first"] = []
    spotify.missing = True
    result = runner.run_cycle()

    if truncated:
        assert not result.ok
        assert "destination preserved" in (result.playlists[0].error or "")
        assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    else:
        assert result.ok
        assert result.playlists[0].wrote is False
        assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") is None
    assert spotify.replacements == [("destination-first", (SER_URI,))]


def test_truncated_catalogue_presence_is_sufficient_for_prewrite(tmp_path: Path) -> None:
    class TruncatedPresenceSpotify(_Spotify):
        truncated = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.truncated:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            del show_id
            self.show_calls += 1
            return catalogue_page(
                [
                    {
                        "uri": SER_URI,
                        "name": SER_TITLE,
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    }
                ],
                offset=offset,
                limit=limit,
                next_url="more",
                total=101,
            )

    spotify = TruncatedPresenceSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
        local_skips_enabled=False,
    )
    assert runner.run_cycle().ok
    spotify.items["destination-first"] = []
    spotify.truncated = True

    result = runner.run_cycle()

    assert result.ok
    assert result.playlists[0].wrote
    assert spotify.replacements == [
        ("destination-first", (SER_URI,)),
        ("destination-first", (SER_URI,)),
    ]


def test_adaptive_retry_is_active_and_does_not_fetch_inside_first_grace(tmp_path: Path) -> None:
    clock = [NOW]

    class InitiallyEmptySpotify(_Spotify):
        available = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if self.available:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            del show_id
            self.show_calls += 1
            return catalogue_page([], offset=offset, limit=limit)

    spotify = InitiallyEmptySpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )

    assert runner.run_cycle().ok
    assert spotify.show_calls == 1
    spotify.available = True
    clock[0] = NOW + timedelta(minutes=14, seconds=59)
    assert runner.run_cycle().ok
    assert spotify.show_calls == 1
    clock[0] = NOW + timedelta(minutes=15)
    assert runner.run_cycle().ok
    assert spotify.show_calls == 2
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI


def test_compatible_source_definitions_share_one_catalogue_fetch(tmp_path: Path) -> None:
    second_source = replace(
        _source(),
        id=SourceId("ser-secondary"),
        endpoint_url="https://example.test/ser-secondary.xml",
    )
    playlist = replace(
        _playlist("first"),
        source_selection=SourceSelection((SourceId("ser"), SourceId("ser-secondary"))),
    )
    config = EngineConfig(
        schema_version=1,
        sources=(_source(), second_source),
        playlists=(playlist,),
    )
    spotify = _Spotify()
    runner = EngineRunner(
        config,
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert result.ok
    assert spotify.show_calls == 1
    assert {outcome.source_id for outcome in result.sources} == {
        SourceId("ser"),
        SourceId("ser-secondary"),
    }


def test_shadow_planner_persistence_failure_does_not_change_spotify_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spotify = _Spotify()
    auth = _Auth()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
        local_skips_enabled=False,
    )

    def fail_catalogue(_context: str) -> None:
        raise PersistenceError("planner unavailable")

    monkeypatch.setattr(runner.planner_journal, "get_catalogue", fail_catalogue)
    result = runner.run_cycle()

    assert result.ok
    assert auth.calls == 1
    assert spotify.show_calls == 1
    assert spotify.playlist_reads
    assert spotify.replacements == [("destination-first", (SER_URI,))]
    assert result.planner_shadow is not None
    assert not result.planner_shadow.state_available


def test_source_failure_carries_forward_recent_durable_match(tmp_path: Path) -> None:
    fail = False
    spotify = _Spotify()

    def fetcher(_url: str) -> bytes:
        if fail:
            raise OSError("source unavailable")
        return _rss()

    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    first = runner.run_cycle()
    fail = True
    second = runner.run_cycle()

    assert first.ok
    assert not second.ok
    assert not second.sources[0].collection_ok
    assert "source unavailable" in (second.sources[0].error or "")
    assert second.playlists[0].ok
    assert second.playlists[0].desired_count == 1
    assert second.playlists[0].wrote is False
    assert spotify.items["destination-first"] == [SER_URI]


def test_title_parser_drift_carries_forward_lkg_without_spotify_work(tmp_path: Path) -> None:
    drift = False
    spotify = _Spotify()
    auth = _Auth()

    def drifted_rss() -> bytes:
        items = (
            ("ser-1", "Las noticias de la SER, 11:01 (30/08/2026)"),
            ("ser-2", "SER DEPORTES - 12:00 (30/08/2026)"),
            ("ser-3", "new unreviewed SER title shape"),
        )
        body = "".join(
            "<item>"
            f"<guid>{native_id}</guid>"
            f"<title>{title}</title>"
            "<pubDate>Sun, 30 Aug 2026 09:05:00 +0000</pubDate>"
            "</item>"
            for native_id, title in items
        )
        return f"<rss><channel>{body}</channel></rss>".encode()

    def fetcher(_url: str) -> bytes:
        return drifted_rss() if drift else _rss()

    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    first = runner.run_cycle()
    persisted_before = runner.store.get_edition(SourceId("ser"), "ser-1")
    provider_counts = (
        auth.calls,
        spotify.show_calls,
        len(spotify.playlist_reads),
        len(spotify.snapshot_read_calls),
        len(spotify.replacements),
    )
    drift = True
    second = runner.run_cycle()

    assert first.ok
    assert persisted_before is not None
    assert persisted_before.title == SER_TITLE
    assert not second.ok
    assert not second.sources[0].collection_ok
    diagnostics = second.sources[0].collection_diagnostics
    assert diagnostics.failure_reason == "title_parser_drift"
    assert diagnostics.rss_title_parser_attempted_count == 3
    assert diagnostics.rss_title_parser_exact_count == 1
    assert diagnostics.rss_title_parser_rejected_count == 2
    assert runner.store.get_edition(SourceId("ser"), "ser-1") == persisted_before
    assert second.playlists[0].ok
    assert second.playlists[0].desired_count == 1
    assert second.playlists[0].wrote is False
    assert spotify.items["destination-first"] == [SER_URI]
    assert (
        auth.calls,
        spotify.show_calls,
        len(spotify.playlist_reads),
        len(spotify.snapshot_read_calls),
        len(spotify.replacements),
    ) == provider_counts


def test_title_parser_drift_does_not_block_independent_healthy_source(
    tmp_path: Path,
) -> None:
    drift_source = replace(
        _source(),
        id=SourceId("ser-drift"),
        endpoint_url="https://example.test/ser-drift.xml",
    )
    healthy_source = replace(
        _source(),
        id=SourceId("ser-healthy"),
        endpoint_url="https://example.test/ser-healthy.xml",
    )
    drift_playlist = replace(
        _playlist("drift"),
        source_selection=SourceSelection((drift_source.id,)),
    )
    healthy_playlist = replace(
        _playlist("healthy"),
        source_selection=SourceSelection((healthy_source.id,)),
    )
    config = EngineConfig(
        schema_version=1,
        sources=(drift_source, healthy_source),
        playlists=(drift_playlist, healthy_playlist),
    )
    drift_body = "".join(
        "<item>"
        f"<guid>drift-{index}</guid>"
        f"<title>{title}</title>"
        "<pubDate>Sun, 30 Aug 2026 09:05:00 +0000</pubDate>"
        "</item>"
        for index, title in enumerate(
            (
                SER_TITLE,
                "SER DEPORTES - 12:00 (30/08/2026)",
                "new unreviewed SER title shape",
            ),
            start=1,
        )
    )
    drift_payload = f"<rss><channel>{drift_body}</channel></rss>".encode()
    spotify = _Spotify()
    auth = _Auth()

    def fetcher(url: str) -> bytes:
        if url == drift_source.endpoint_url:
            return drift_payload
        assert url == healthy_source.endpoint_url
        return _rss()

    runner = EngineRunner(
        config,
        _store(tmp_path),
        auth,
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    by_source = {outcome.source_id: outcome for outcome in result.sources}
    assert not by_source[drift_source.id].collection_ok
    assert by_source[drift_source.id].collection_diagnostics.failure_reason == "title_parser_drift"
    assert by_source[healthy_source.id].collection_ok
    assert by_source[healthy_source.id].matching_ok is True

    by_playlist = {outcome.playlist_id: outcome for outcome in result.playlists}
    assert by_playlist[drift_playlist.id].wrote is None
    assert by_playlist[healthy_playlist.id].ok
    assert by_playlist[healthy_playlist.id].wrote is True
    assert spotify.items["destination-healthy"] == [SER_URI]
    assert spotify.replacements == [("destination-healthy", (SER_URI,))]
    assert "destination-drift" not in spotify.playlist_reads
    assert "destination-drift" not in spotify.snapshot_read_calls
    assert auth.calls == 1
    assert spotify.show_calls == 1


def test_first_title_parser_drift_preserves_existing_destination_without_lkg_or_spotify_io(
    tmp_path: Path,
) -> None:
    spotify = _Spotify()
    auth = _Auth()
    existing_uri = "spotify:episode:0000000000000000000002"
    spotify.items["destination-first"] = [existing_uri]

    body = "".join(
        "<item>"
        f"<guid>ser-{index}</guid>"
        f"<title>{title}</title>"
        "<pubDate>Sun, 30 Aug 2026 09:05:00 +0000</pubDate>"
        "</item>"
        for index, title in enumerate(
            (
                SER_TITLE,
                "SER DEPORTES - 12:00 (30/08/2026)",
                "new unreviewed SER title shape",
            ),
            start=1,
        )
    )
    payload = f"<rss><channel>{body}</channel></rss>".encode()

    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: payload,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    assert result.sources[0].collection_diagnostics.failure_reason == "title_parser_drift"
    assert result.playlists[0].wrote is None
    assert "destination preserved" in (result.playlists[0].error or "")
    assert spotify.items["destination-first"] == [existing_uri]
    assert auth.calls == 0
    assert spotify.show_calls == 0
    assert spotify.playlist_reads == []
    assert spotify.snapshot_read_calls == []
    assert spotify.replacements == []


def test_first_source_failure_preserves_existing_destination_without_lkg(
    tmp_path: Path,
) -> None:
    spotify = _Spotify()
    existing_uri = "spotify:episode:0000000000000000000002"
    spotify.items["destination-first"] = [existing_uri]
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: (_ for _ in ()).throw(OSError("source unavailable")),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    assert not result.playlists[0].ok
    assert result.playlists[0].wrote is None
    assert "destination preserved" in (result.playlists[0].error or "")
    assert spotify.items["destination-first"] == [existing_uri]
    assert spotify.playlist_reads == []
    assert spotify.replacements == []


def test_first_matching_failure_preserves_existing_destination_without_lkg(
    tmp_path: Path,
) -> None:
    existing_uri = "spotify:episode:0000000000000000000002"

    class MatchingUnavailableSpotify(_Spotify):
        def show_episodes(
            self,
            show_id: str,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> dict[str, Any]:
            del show_id, limit, offset
            raise SpotifyTransportError("simulated catalogue outage")

    spotify = MatchingUnavailableSpotify()
    spotify.items["destination-first"] = [existing_uri]
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    assert result.sources[0].matching_ok is False
    assert not result.playlists[0].ok
    assert result.playlists[0].wrote is None
    assert "destination preserved" in (result.playlists[0].error or "")
    assert spotify.items["destination-first"] == [existing_uri]
    assert spotify.playlist_reads == []
    assert spotify.replacements == []


def test_playlist_failure_does_not_block_other_destination(tmp_path: Path) -> None:
    spotify = _Spotify(fail_playlist="destination-first")
    runner = EngineRunner(
        _config("first", "second"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    by_id = {outcome.playlist_id: outcome for outcome in result.playlists}
    assert not by_id[PlaylistId("first")].ok
    assert by_id[PlaylistId("second")].ok
    assert spotify.items["destination-second"] == [SER_URI]


def test_polish_replace_502_does_not_block_rss_or_healthy_destination(tmp_path: Path) -> None:
    class Replace502Spotify(_Spotify):
        def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
            if playlist_id == "destination-first":
                raise SpotifyApiError(502, "provider raw body must stay private")
            return super().replace_playlist_items(playlist_id, uris)

    spotify = Replace502Spotify()
    fetch_calls: list[str] = []

    def fetcher(url: str) -> bytes:
        fetch_calls.append(url)
        return _rss()

    result = EngineRunner(
        _config("first", "second"),
        _store(tmp_path),
        _Auth(),
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    ).run_cycle()

    by_id = {outcome.playlist_id: outcome for outcome in result.playlists}
    assert fetch_calls == [SER_URL]
    assert not by_id[PlaylistId("first")].ok
    assert by_id[PlaylistId("first")].write_outcome == "unknown"
    assert by_id[PlaylistId("second")].ok
    assert spotify.items["destination-second"] == [SER_URI]


def test_legacy_malformed_mapping_does_not_abort_or_block_later_playlist(tmp_path: Path) -> None:
    class EmptyCatalogueSpotify(_Spotify):
        def show_episodes(
            self,
            show_id: str,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> dict[str, Any]:
            del show_id
            return catalogue_page([], offset=offset, limit=limit)

    store = _store(tmp_path)
    spotify = EmptyCatalogueSpotify()
    runner = EngineRunner(
        _config("first", "second"),
        store,
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )
    initial = runner.run_cycle()
    assert initial.ok
    store.set_match_state(
        SourceId("ser"),
        "ser-1",
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:legacy-invalid",
        spotify_duration_seconds=60,
        diagnostics="legacy mapping",
        updated_at=NOW,
    )

    result = runner.run_cycle()

    assert result.ok
    assert all(outcome.ok for outcome in result.playlists)
    state = store.get_match_state(SourceId("ser"), "ser-1")
    assert state is not None
    assert state.status is MatchStatus.PENDING
    assert state.spotify_episode_uri is None


def test_auth_failure_is_reported_without_empty_success(tmp_path: Path) -> None:
    auth = _Auth(SpotifyReauthorizationRequired("Spotify authorization is required"))
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: pytest.fail("client must not be created"),
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    assert result.error == "Spotify authorization is required"
    assert result.sources[0].collection_ok
    assert result.playlists[0].error == "Spotify authorization is required"
    assert auth.calls == 1


def test_access_token_is_consumed_but_never_exposed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    access_token = "access-token-sentinel"
    spotify = _Spotify(fail_playlist="destination-first")

    class SentinelAuth:
        def get_access_token(self, *, now: datetime | None = None) -> str:
            assert now is not None
            return access_token

    def client_factory(token: str) -> _Spotify:
        assert token == access_token
        return spotify

    result = EngineRunner(
        _config("first"),
        _store(tmp_path),
        SentinelAuth(),
        fetcher=lambda _url: _rss(),
        client_factory=client_factory,
        clock=lambda: NOW,
    ).run_cycle()

    captured = capsys.readouterr()
    assert not result.ok
    assert access_token not in repr(result)
    assert access_token not in captured.out
    assert access_token not in captured.err


def test_concurrent_cycle_is_rejected(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def fetcher(_url: str) -> bytes:
        entered.set()
        assert release.wait(timeout=2)
        return _rss()

    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=fetcher,
        client_factory=lambda _token: _Spotify(),
        clock=lambda: NOW,
    )
    thread = threading.Thread(target=runner.run_cycle)
    thread.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(EngineCycleAlreadyRunning):
            runner.run_cycle()
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_scheduler_wake_runs_early_and_stop_prevents_another_cycle() -> None:
    calls = 0
    first_done = threading.Event()
    second_done = threading.Event()

    class Runner:
        def run_cycle(self) -> EngineCycleResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_done.set()
            if calls == 2:
                second_done.set()
            return EngineCycleResult(NOW, NOW, True, (), ())

    status = OperationalStatus(configured=True)
    scheduler = EngineScheduler(
        Runner(),
        status,
        interval=timedelta(hours=1),
        clock=lambda: NOW,
    )
    stop = threading.Event()
    thread = threading.Thread(target=scheduler.run, args=(stop,))
    thread.start()
    assert first_done.wait(timeout=2)
    scheduler.wake()
    assert second_done.wait(timeout=2)
    stop.set()
    scheduler.wake()
    thread.join(timeout=2)

    assert calls == 2
    assert not thread.is_alive()
    assert status.snapshot().next_run_at is None


def test_scheduler_stop_event_interrupts_wait_without_explicit_wake() -> None:
    calls = 0
    first_done = threading.Event()

    class Runner:
        def run_cycle(self) -> EngineCycleResult:
            nonlocal calls
            calls += 1
            first_done.set()
            return EngineCycleResult(NOW, NOW, True, (), ())

    status = OperationalStatus(configured=True)
    scheduler = EngineScheduler(
        Runner(),
        status,
        interval=timedelta(hours=1),
        clock=lambda: NOW,
    )
    stop = threading.Event()
    thread = threading.Thread(target=scheduler.run, args=(stop,))
    thread.start()
    assert first_done.wait(timeout=2)

    stop.set()
    thread.join(timeout=2)

    assert calls == 1
    assert not thread.is_alive()
    assert status.snapshot().next_run_at is None


def test_missing_spotify_duration_preserves_existing_destination_without_write(
    tmp_path: Path,
) -> None:
    existing_uri = "spotify:episode:0000000000000000000002"

    class MissingDurationSpotify(_Spotify):
        def show_episodes(
            self,
            show_id: str,
            *,
            limit: int = 50,
            offset: int = 0,
        ) -> dict[str, Any]:
            page = super().show_episodes(show_id, limit=limit, offset=offset)
            item = page["items"][0]
            assert isinstance(item, dict)
            item.pop("duration_ms")
            return page

    spotify = MissingDurationSpotify()
    spotify.items["destination-first"] = [existing_uri]
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    assert not result.ok
    assert result.sources[0].matching_ok is True
    assert not result.playlists[0].ok
    assert result.playlists[0].wrote is None
    assert "Spotify duration unavailable" in (result.playlists[0].error or "")
    assert spotify.items["destination-first"] == [existing_uri]
    assert spotify.playlist_reads == []
    assert spotify.replacements == []


class _EmptyCatalogueSpotify(_Spotify):
    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "ser-show"
        self.show_calls += 1
        return catalogue_page([], offset=offset, limit=limit)


class _FailingCatalogueSpotify(_Spotify):
    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "ser-show"
        self.show_calls += 1
        raise SpotifyTransportError("catalogue failure sentinel")


class _TwoPageSpotify(_Spotify):
    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "ser-show"
        self.show_calls += 1
        items = [
            {
                "uri": SER_URI,
                "name": SER_TITLE,
                "release_date": "2026-08-30",
                "release_date_precision": "day",
                "duration_ms": 60_000,
            }
        ]
        items.extend(
            {
                "uri": f"spotify:episode:{index:022d}",
                "name": f"Other {index}",
                "release_date": "2026-08-30",
                "release_date_precision": "day",
                "duration_ms": 60_000,
            }
            for index in range(2, 52)
        )
        page_items = items[offset : offset + limit]
        next_url = (
            "https://api.spotify.test/catalogue?offset=50" if offset + limit < len(items) else None
        )
        return catalogue_page(
            page_items,
            offset=offset,
            limit=limit,
            next_url=next_url,
            total=len(items),
        )


def _rss_two() -> bytes:
    return (
        "<rss><channel>"
        "<item><guid>ser-1</guid>"
        f"<title>{SER_TITLE}</title>"
        "<pubDate>Sun, 30 Aug 2026 09:05:00 +0000</pubDate></item>"
        "<item><guid>ser-2</guid>"
        "<title>Las noticias de la SER, 10:00 (30/08/2026)</title>"
        "<pubDate>Sun, 30 Aug 2026 08:05:00 +0000</pubDate></item>"
        "</channel></rss>"
    ).encode()


def _rss_changed() -> bytes:
    return (
        b"<rss><channel><item>"
        b"<guid>ser-1</guid>"
        b"<title>Las noticias de la SER, 10:30 (30/08/2026)</title>"
        b"<pubDate>Sun, 30 Aug 2026 09:05:00 +0000</pubDate>"
        b"</item></channel></rss>"
    )


def test_source_matching_diagnostics_stable_local_only(tmp_path: Path) -> None:
    auth = _Auth()
    spotify = _Spotify()
    current = NOW
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    first = runner.run_cycle()
    first_auth_calls = auth.calls
    first_show_calls = spotify.show_calls
    assert first.ok

    current = NOW + timedelta(minutes=10)
    second = runner.run_cycle()

    assert second.ok
    assert second.provider_session_created is False
    assert auth.calls == first_auth_calls
    assert spotify.show_calls == first_show_calls
    diagnostic = second.sources[0].matching_diagnostics
    assert diagnostic.matching_work_count == 0
    assert diagnostic.catalogue_audit_due is False
    assert diagnostic.catalogue_requested is False
    assert diagnostic.catalogue_inspected is False
    assert diagnostic.catalogue_request_count == 0


def test_source_matching_diagnostics_one_retry_due(tmp_path: Path) -> None:
    spotify = _EmptyCatalogueSpotify()
    current = NOW
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    assert runner.run_cycle().ok
    calls = spotify.show_calls
    current = NOW + timedelta(minutes=15)

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_work_count == 1
    assert diagnostic.matching_reason_retry_due_count == 1
    assert diagnostic.unresolved_relevant_count == 1
    assert diagnostic.catalogue_audit_due is False
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is True
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == calls + 1


def test_source_matching_diagnostics_two_retries_share_one_catalogue(tmp_path: Path) -> None:
    spotify = _EmptyCatalogueSpotify()
    current = NOW
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss_two(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    assert runner.run_cycle().ok
    calls = spotify.show_calls
    current = NOW + timedelta(minutes=15)

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_work_count == 2
    assert diagnostic.matching_reason_retry_due_count == 2
    assert diagnostic.unresolved_relevant_count == 2
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == calls + 1


def test_source_matching_diagnostics_new_and_retry_share_catalogue(tmp_path: Path) -> None:
    spotify = _EmptyCatalogueSpotify()
    current = NOW
    two = False

    def fetcher(_url: str) -> bytes:
        return _rss_two() if two else _rss()

    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    assert runner.run_cycle().ok
    calls = spotify.show_calls
    two = True
    current = NOW + timedelta(minutes=15)

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_work_count == 2
    assert diagnostic.matching_reason_new_match_count == 1
    assert diagnostic.matching_reason_retry_due_count == 1
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == calls + 1


def test_source_matching_diagnostics_input_changed(tmp_path: Path) -> None:
    spotify = _EmptyCatalogueSpotify()
    current = NOW
    changed = False

    def fetcher(_url: str) -> bytes:
        return _rss_changed() if changed else _rss()

    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    assert runner.run_cycle().ok
    calls = spotify.show_calls
    changed = True
    current = NOW + timedelta(minutes=5)

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_work_count == 1
    assert diagnostic.matching_reason_input_changed_count == 1
    assert diagnostic.matching_reason_retry_due_count == 0
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == calls + 1


def test_source_matching_diagnostics_catalogue_audit_only(tmp_path: Path) -> None:
    spotify = _Spotify()
    current = NOW
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    assert runner.run_cycle().ok
    calls = spotify.show_calls
    current = NOW + timedelta(hours=6)

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_work_count == 0
    assert diagnostic.catalogue_audit_due is True
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is True
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == calls + 1


def test_source_matching_diagnostics_two_page_catalogue(tmp_path: Path) -> None:
    spotify = _TwoPageSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_reason_new_match_count == 1
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is True
    assert diagnostic.catalogue_request_count == 2
    assert spotify.show_calls == 2


def test_source_matching_diagnostics_active_cooldown_preserves_plan_without_calls(
    tmp_path: Path,
) -> None:
    auth = _Auth()
    spotify = _EmptyCatalogueSpotify()
    current = NOW
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        auth,
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    assert runner.run_cycle().ok
    show_calls = spotify.show_calls
    auth_calls = auth.calls
    current = NOW + timedelta(minutes=15)
    runner.rate_limit_journal.activate(observed_at=current, retry_after_seconds=600)

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_reason_retry_due_count == 1
    assert diagnostic.catalogue_requested is False
    assert diagnostic.catalogue_inspected is False
    assert diagnostic.catalogue_request_count == 0
    assert spotify.show_calls == show_calls
    assert auth.calls == auth_calls
    assert result.provider_session_created is False


def test_source_matching_diagnostics_catalogue_error_counts_real_delegation(
    tmp_path: Path,
) -> None:
    spotify = _FailingCatalogueSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_reason_new_match_count == 1
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is False
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == 1
    assert result.sources[0].matching_ok is False


def test_source_matching_diagnostics_include_prewrite_catalogue_without_double_count(
    tmp_path: Path,
) -> None:
    spotify = _Spotify()
    current = NOW
    runner = EngineRunner(
        _config("first", "second"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    assert runner.run_cycle().ok
    calls = spotify.show_calls
    current = NOW + timedelta(minutes=10)
    spotify.items["destination-first"] = []
    spotify.items["destination-second"] = []
    runner.planner_journal.begin_destination_verification("first")
    runner.planner_journal.begin_destination_verification("second")

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_work_count == 0
    assert diagnostic.catalogue_audit_due is False
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is True
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == calls + 1


class _FailingSecondPageSpotify(_TwoPageSpotify):
    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if offset == 50:
            assert show_id == "ser-show"
            self.show_calls += 1
            raise SpotifyTransportError("second catalogue page failure sentinel")
        return super().show_episodes(show_id, limit=limit, offset=offset)


def test_source_matching_diagnostics_second_page_failure_counts_both_delegations(
    tmp_path: Path,
) -> None:
    spotify = _FailingSecondPageSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: NOW,
    )

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert result.sources[0].matching_ok is False
    assert diagnostic.matching_reason_new_match_count == 1
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is False
    assert diagnostic.catalogue_request_count == 2
    assert spotify.show_calls == 2


def test_source_matching_diagnostics_prewrite_survive_fatal_persistence_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spotify = _Spotify()
    store = _store(tmp_path)
    current = NOW
    runner = EngineRunner(
        _config("first"),
        store,
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: current,
    )
    assert runner.run_cycle().ok
    prior_show_calls = spotify.show_calls

    current = NOW + timedelta(minutes=10)
    spotify.items["destination-first"] = []
    runner.planner_journal.begin_destination_verification("first")
    original_record_playlist_run = store.record_playlist_run

    def fail_after_prewrite(*args: Any, **kwargs: Any) -> None:
        if spotify.show_calls > prior_show_calls:
            raise PersistenceError("post-prewrite persistence failure")
        original_record_playlist_run(*args, **kwargs)

    monkeypatch.setattr(store, "record_playlist_run", fail_after_prewrite)

    result = runner.run_cycle()

    diagnostic = result.sources[0].matching_diagnostics
    assert result.ok is False
    assert result.error == "post-prewrite persistence failure"
    assert diagnostic.matching_work_count == 0
    assert diagnostic.catalogue_audit_due is False
    assert diagnostic.catalogue_requested is True
    assert diagnostic.catalogue_inspected is True
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == prior_show_calls + 1


def test_input_changed_ambiguity_preserves_cached_mapping_and_both_playlists(
    tmp_path: Path,
) -> None:
    clock = [NOW]

    class AmbiguousSpotify(_Spotify):
        ambiguous = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.ambiguous:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            self.show_calls += 1
            return catalogue_page(
                [
                    {
                        "uri": SER_URI,
                        "name": SER_TITLE,
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    },
                    {
                        "uri": "spotify:episode:" + "2" * 22,
                        "name": SER_TITLE,
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 61_000,
                    },
                ],
                offset=offset,
                limit=limit,
            )

    spotify = AmbiguousSpotify()
    runner = EngineRunner(
        _config("first", "second"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    writes_before = tuple(spotify.replacements)
    runner.planner_journal.observe_match_input(
        source_id="ser",
        source_native_id="ser-1",
        input_fingerprint="b" * 64,
        observed_at=NOW,
    )
    spotify.ambiguous = True
    clock[0] = NOW + timedelta(minutes=10)

    result = runner.run_cycle()

    assert result.ok
    assert result.sources[0].matching_diagnostics.matching_reason_input_changed_count == 1
    state = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert state is not None and state.status is MatchStatus.MATCHED
    assert state.spotify_episode_uri == SER_URI
    assert spotify.items["destination-first"] == [SER_URI]
    assert spotify.items["destination-second"] == [SER_URI]
    assert tuple(spotify.replacements) == writes_before
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert recovery is not None


def test_stale_match_input_inconclusive_catalogue_uses_six_hour_recovery_across_restart(
    tmp_path: Path,
) -> None:
    clock = [NOW]

    class TruncatedMissingSpotify(_Spotify):
        bounded = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.bounded:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            self.show_calls += 1
            items = [
                {
                    "uri": f"spotify:episode:{offset + index + 1000:022d}",
                    "name": "unrelated",
                    "release_date": "2026-08-30",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
                for index in range(50)
            ]
            return catalogue_page(
                items,
                offset=offset,
                limit=limit,
                next_url="more",
                total=101,
            )

    spotify = TruncatedMissingSpotify()
    store = _store(tmp_path)
    runner = EngineRunner(
        _config("first"),
        store,
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    assert spotify.show_calls == 1
    runner.planner_journal.observe_match_input(
        source_id="ser",
        source_native_id="ser-1",
        input_fingerprint="b" * 64,
        observed_at=NOW,
    )
    spotify.bounded = True

    clock[0] = NOW + timedelta(minutes=10)
    first = runner.run_cycle()
    assert first.ok
    assert spotify.show_calls == 3
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert recovery is not None
    calls_after_first = spotify.show_calls

    clock[0] = NOW + timedelta(minutes=20)
    assert runner.run_cycle().ok
    assert spotify.show_calls == calls_after_first

    clock[0] = NOW + timedelta(minutes=30)
    restarted = EngineRunner(
        _config("first"),
        SQLiteStore(store.path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert restarted.run_cycle().ok
    assert spotify.show_calls == calls_after_first

    clock[0] = NOW + timedelta(hours=6, minutes=10)
    due = restarted.run_cycle()
    assert due.ok
    assert spotify.show_calls == calls_after_first + 2
    assert due.sources[0].matching_diagnostics.matching_reason_evidence_recovery_count == 1


def test_new_input_breaks_matched_recovery_backoff_immediately(tmp_path: Path) -> None:
    clock = [NOW]

    class TruncatedMissingSpotify(_Spotify):
        bounded = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.bounded:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            self.show_calls += 1
            items = [
                {
                    "uri": f"spotify:episode:{offset + index + 1000:022d}",
                    "name": "unrelated",
                    "release_date": "2026-08-30",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
                for index in range(50)
            ]
            return catalogue_page(
                items,
                offset=offset,
                limit=limit,
                next_url="more",
                total=101,
            )

    spotify = TruncatedMissingSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    runner.planner_journal.observe_match_input(
        source_id="ser",
        source_native_id="ser-1",
        input_fingerprint="b" * 64,
        observed_at=NOW,
    )
    spotify.bounded = True
    clock[0] = NOW + timedelta(minutes=10)
    assert runner.run_cycle().ok
    calls_after_first = spotify.show_calls

    clock[0] = NOW + timedelta(minutes=20)
    assert runner.run_cycle().ok
    assert spotify.show_calls == calls_after_first

    runner.config = replace(
        runner.config,
        sources=(replace(_source(), spotify_release_delay_days=1),),
    )
    clock[0] = NOW + timedelta(minutes=30)
    changed = runner.run_cycle()

    assert changed.ok
    assert spotify.show_calls == calls_after_first + 2
    assert changed.sources[0].matching_diagnostics.matching_reason_input_changed_count == 1


def test_unknown_provider_duration_keeps_known_duration_and_enters_recovery_backoff(
    tmp_path: Path,
) -> None:
    clock = [NOW]

    class UnknownDurationSpotify(_Spotify):
        unknown_duration = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            page = super().show_episodes(show_id, limit=limit, offset=offset)
            if self.unknown_duration:
                page["items"][0]["duration_ms"] = None
            return page

    spotify = UnknownDurationSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    runner.planner_journal.clear_match_input("ser", "ser-1")
    spotify.unknown_duration = True
    calls_before = spotify.show_calls

    clock[0] = NOW + timedelta(minutes=10)
    first = runner.run_cycle()
    assert first.ok
    assert spotify.show_calls == calls_before + 1
    state = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert state is not None
    assert state.status is MatchStatus.MATCHED
    assert state.spotify_episode_uri == SER_URI
    assert state.spotify_duration_seconds == 60
    assert runner.planner_journal.get_matched_recovery("ser", "ser-1") is not None

    clock[0] = NOW + timedelta(minutes=20)
    assert runner.run_cycle().ok
    assert spotify.show_calls == calls_before + 1


def test_clock_rollback_rejected_repair_never_publishes_match_input_and_recovers_later(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    replacement_uri = "spotify:episode:" + "9" * 22

    class ReplacementSpotify(_Spotify):
        replacement_catalogue = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.replacement_catalogue:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            assert limit == 50
            assert offset == 0
            self.show_calls += 1
            return catalogue_page(
                [
                    {
                        "uri": SER_URI,
                        "name": "contenido que no representa la edición",
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    },
                    {
                        "uri": replacement_uri,
                        "name": SER_TITLE,
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 61_000,
                    },
                ],
                offset=offset,
                limit=limit,
            )

    spotify = ReplacementSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    assert runner.planner_journal.get_match_input("ser", "ser-1") is not None
    runner.planner_journal.clear_match_input("ser", "ser-1")
    spotify.replacement_catalogue = True
    writes_before = tuple(spotify.replacements)

    # Roll the runner clock behind the durable 10:00 match. The catalogue proves A stale and B
    # correct, but SQLite must reject a repair stamped 09:50. The engine must not publish current
    # evidence for B (or for A) and must not mutate the destination from the durable A state.
    clock[0] = NOW - timedelta(minutes=10)
    rolled_back = runner.run_cycle()
    # A rejected authoritative repair preserves the destination and marks reconciliation unsafe.
    assert not rolled_back.ok
    durable = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert durable is not None
    assert durable.status is MatchStatus.MATCHED
    assert durable.spotify_episode_uri == SER_URI
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    recovery = runner.planner_journal.get_matched_recovery("ser", "ser-1")
    assert recovery is not None
    assert recovery.last_attempt_at == NOW - timedelta(minutes=10)
    assert recovery.next_retry_at == NOW + timedelta(hours=5, minutes=50)
    assert spotify.items["destination-first"] == [SER_URI]
    assert tuple(spotify.replacements) == writes_before
    calls_after_rejection = spotify.show_calls

    # Merely catching up past the durable mapping timestamp must not recreate ten-minute polling.
    # With no destination reconciliation needed, this quiet cycle may complete successfully.
    clock[0] = NOW + timedelta(minutes=1)
    waiting = runner.run_cycle()
    assert waiting.ok
    assert spotify.show_calls == calls_after_rejection
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    assert spotify.items["destination-first"] == [SER_URI]
    assert tuple(spotify.replacements) == writes_before

    clock[0] = recovery.next_retry_at
    recovered = runner.run_cycle()
    assert recovered.ok
    durable = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert durable is not None
    assert durable.status is MatchStatus.MATCHED
    assert durable.spotify_episode_uri == replacement_uri
    assert durable.spotify_duration_seconds == 61
    assert runner.planner_journal.get_match_input("ser", "ser-1") is not None
    assert runner.planner_journal.get_matched_recovery("ser", "ser-1") is None
    assert spotify.items["destination-first"] == [replacement_uri]


def test_prewrite_positive_presence_stops_after_first_catalogue_page(tmp_path: Path) -> None:
    clock = [NOW]

    class TwoPagePrewriteSpotify(_Spotify):
        prewrite_two_page = False

        def show_episodes(
            self, show_id: str, *, limit: int = 50, offset: int = 0
        ) -> dict[str, Any]:
            if not self.prewrite_two_page:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            assert limit == 50
            self.show_calls += 1
            if offset != 0:
                raise AssertionError(
                    "prewrite presence validation should stop after the first page"
                )
            items = [
                {
                    "uri": SER_URI,
                    "name": SER_TITLE,
                    "release_date": "2026-08-30",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
            ] + [
                {
                    "uri": f"spotify:episode:{index + 100:022d}",
                    "name": f"unrelated {index}",
                    "release_date": "2026-08-30",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
                for index in range(49)
            ]
            return catalogue_page(
                items,
                offset=offset,
                limit=limit,
                next_url="more",
                total=51,
            )

    spotify = TwoPagePrewriteSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    prior_show_calls = spotify.show_calls

    clock[0] = NOW + timedelta(minutes=10)
    spotify.prewrite_two_page = True
    spotify.items["destination-first"] = []
    runner.planner_journal.begin_destination_verification("first")

    result = runner.run_cycle()

    assert result.ok
    diagnostic = result.sources[0].matching_diagnostics
    assert diagnostic.matching_work_count == 0
    assert diagnostic.catalogue_request_count == 1
    assert spotify.show_calls == prior_show_calls + 1
    assert spotify.items["destination-first"] == [SER_URI]
