from __future__ import annotations

from datetime import timedelta

from test_engine import NOW
from test_shadow_planner_review import SpySpotify, setup

from news_bulletin_playlist.engine import DestinationRuntimeLeaseRegistry
from news_bulletin_playlist.spotify.client import SpotifyTransportError


def test_due_clean_destination_audit_uses_one_snapshot_and_no_item_read(tmp_path) -> None:
    runner, provider, _, _ = setup(tmp_path)
    assert runner.run_cycle().ok
    provider.trace.clear()
    runner.clock = lambda: NOW + timedelta(hours=6)

    result = runner.run_cycle()

    assert result.ok
    assert result.destinations_remote == 1
    destination_calls = [entry[0] for entry in provider.trace if entry[0] != "catalogue"]
    assert destination_calls == ["snapshot"]
    state = runner.planner_journal.get_destination("first")
    assert state is not None
    assert state.last_verified_at == NOW + timedelta(hours=6)
    assert state.next_audit_at == NOW + timedelta(hours=12)


def test_missing_process_lease_before_audit_does_not_use_snapshot_shortcut(tmp_path) -> None:
    runner, provider, _, _ = setup(tmp_path)
    assert runner.run_cycle().ok
    provider.trace.clear()
    runner.destination_leases = DestinationRuntimeLeaseRegistry()
    runner.clock = lambda: NOW + timedelta(hours=1)

    result = runner.run_cycle()

    assert result.ok
    assert result.destinations_remote == 1
    destination_calls = [entry[0] for entry in provider.trace if entry[0] != "catalogue"]
    assert destination_calls == ["items"]


def test_snapshot_audit_failure_keeps_due_durable_evidence_but_publishes_no_lease(
    tmp_path,
) -> None:
    class FailingAuditSpotify(SpySpotify):
        fail_snapshot = False

        def playlist_snapshot(self, *args, **kwargs):
            if self.fail_snapshot:
                self.trace.append(("snapshot", args, kwargs))
                raise SpotifyTransportError("snapshot audit failure")
            return super().playlist_snapshot(*args, **kwargs)

    provider = FailingAuditSpotify()
    runner, provider, _, _ = setup(tmp_path, spotify=provider)
    assert runner.run_cycle().ok
    before = runner.planner_journal.get_destination("first")
    assert before is not None
    assert before.confirmed_desired_fingerprint is not None
    provider.trace.clear()
    provider.fail_snapshot = True
    runner.clock = lambda: NOW + timedelta(hours=6)

    result = runner.run_cycle()

    assert not result.ok
    assert [entry[0] for entry in provider.trace if entry[0] != "catalogue"] == ["snapshot"]
    after = runner.planner_journal.get_destination("first")
    assert after is not None
    assert after.dirty is False
    assert after.unknown is False
    assert after.confirmed_desired_fingerprint == before.confirmed_desired_fingerprint
    assert after.last_verified_at == before.last_verified_at
    assert after.next_audit_at == before.next_audit_at
    expected = runner._destination_verification_context(
        runner.config.playlists[0],
        (provider.items["destination-first"][0],),
    )
    assert expected is not None
    assert not runner.destination_leases.allows(
        runner.config.playlists[0].id,
        expected,
        monotonic_now=runner.monotonic_clock(),
    )
