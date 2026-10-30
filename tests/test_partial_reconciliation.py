"""Add-only reconciliation must never replace, evict, duplicate or blindly retry."""

from __future__ import annotations

from contextlib import nullcontext

import pytest
from test_reconciliation import _FakeSpotify

from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.partial_reconciliation import (
    PartialAddResult,
    reconcile_partial_additions,
)
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.reconciliation import SpotifyReconciliationError
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError

ID = PlaylistId("dependent")


def uri(number: int) -> str:
    return f"spotify:episode:{number:022d}"


class Client(_FakeSpotify):
    def __init__(self, existing=()):
        super().__init__({"dependent": list(existing)})
        self.additions = []
        self.failure = ""
        self.change_snapshot_during_read = False

    def playlist_snapshot(self, playlist_id):
        if self.change_snapshot_during_read:
            self.snapshot += "-changed"
        return super().playlist_snapshot(playlist_id)

    def add_playlist_items(self, playlist_id, uris, *, position=0):
        assert position == 0
        self.additions.append(tuple(uris))
        if self.failure in {"400", "429", "500_before", "transport_before"}:
            if self.failure == "transport_before":
                raise SpotifyTransportError("injected transport error")
            status = {"400": 400, "429": 429, "500_before": 503}[self.failure]
            raise SpotifyApiError(status, "test-only error")
        self.states[playlist_id] = list(uris) + self.states[playlist_id]
        self.snapshot = f"snapshot-after-{len(self.additions)}"
        if self.failure == "500_after":
            raise SpotifyApiError(503, "test-only error")
        if self.failure == "transport_after":
            raise SpotifyTransportError("injected transport error")
        if self.failure == "malformed_response":
            return {}
        if self.failure == "bad_readback":
            self.states[playlist_id] = list(reversed(self.states[playlist_id]))
        return {"snapshot_id": self.snapshot}


def apply(client, store, candidates, *, maximum=100):
    return reconcile_partial_additions(
        client,
        playlist_id=ID,
        destination_id="dependent",
        candidate_uris=candidates,
        max_items=maximum,
        store=store,
        mutation_guard=nullcontext,
    )


@pytest.fixture
def store(tmp_path):
    database = SQLiteStore(tmp_path / "state.sqlite3")
    database.initialize()
    return database


def test_new_episodes_insert_at_front_without_touching_old_sequence(store):
    client = Client((uri(1), uri(2), uri(2)))
    result = apply(client, store, (uri(4), uri(3), uri(1)), maximum=8)
    assert result == PartialAddResult("added", 2, 5)
    assert client.additions == [(uri(4), uri(3))]
    assert client.states["dependent"] == [uri(4), uri(3), uri(1), uri(2), uri(2)]
    assert not client.writes
    second = apply(client, store, (uri(4), uri(3), uri(1)), maximum=8)
    assert second.status == "unchanged"
    assert client.additions == [(uri(4), uri(3))]


def test_capacity_is_all_or_nothing_and_never_evicts(store):
    client = Client((uri(1), uri(2)))
    result = apply(client, store, (uri(4), uri(3)), maximum=3)
    assert result.status == "capacity"
    assert not client.additions and not client.writes
    assert client.states["dependent"] == [uri(1), uri(2)]


def test_identical_candidates_are_not_inserted(store):
    client = Client((uri(1), uri(2)))
    result = apply(client, store, (uri(1), uri(2)))
    assert result.status == "unchanged"
    assert not client.additions and not client.writes


@pytest.mark.parametrize("candidate", [("bad-uri",), (uri(1), uri(1))])
def test_invalid_or_duplicate_input_is_rejected_without_post(store, candidate):
    client = Client()
    with pytest.raises(ValueError):
        apply(client, store, candidate)
    assert not client.additions


def test_unknown_destination_blocks_addition(store):
    class Unavailable(Client):
        def playlist_items(self, playlist_id, *, limit=50, offset=0):
            return {"items": [{"item": None}], "next": None, "total": 1}

    client = Unavailable()
    with pytest.raises(SpotifyReconciliationError, match="unavailable"):
        apply(client, store, (uri(9),))
    assert not client.additions


def test_snapshot_drift_blocks_addition_before_post(store):
    client = Client()
    client.change_snapshot_during_read = True
    with pytest.raises(SpotifyReconciliationError, match="snapshot changed"):
        apply(client, store, (uri(9),))
    assert not client.additions


@pytest.mark.parametrize("mode", ["500_after", "transport_after"])
def test_ambiguous_post_with_exact_readback_is_not_retried(store, mode):
    client = Client((uri(1),))
    client.failure = mode
    result = apply(client, store, (uri(9),))
    assert result.status == "added"
    assert len(client.additions) == 1
    assert client.states["dependent"] == [uri(9), uri(1)]
    assert apply(client, store, (uri(9),)).status == "unchanged"
    assert len(client.additions) == 1


@pytest.mark.parametrize("mode", ["500_before", "transport_before", "malformed_response"])
def test_uncertain_post_fences_future_retries_across_restart(store, mode):
    client = Client((uri(1),))
    client.failure = mode
    with pytest.raises((SpotifyApiError, SpotifyTransportError, SpotifyReconciliationError)):
        apply(client, store, (uri(9),))
    assert len(client.additions) == 1
    client.failure = ""
    expected_status = "unchanged" if mode == "malformed_response" else "pending"
    assert apply(client, store, (uri(9),)).status == expected_status
    assert len(client.additions) == 1
    # An exact observation of the expected state resolves the durable intent.
    client.states["dependent"] = [uri(9), uri(1)]
    client.snapshot = "confirmed"
    assert apply(client, store, (uri(9),)).status == "unchanged"
    assert len(client.additions) == 1


@pytest.mark.parametrize("mode", ["400", "429"])
def test_rejected_post_does_not_retain_ambiguous_intent(store, mode):
    client = Client((uri(1),))
    client.failure = mode
    with pytest.raises(SpotifyApiError):
        apply(client, store, (uri(9),))
    assert client.states["dependent"] == [uri(1)]
    assert not client.writes
    # The production rate-limit wrapper independently stops requests after 429.
    client.failure = ""
    assert apply(client, store, (uri(9),)).status == "added"


def test_post_readback_mismatch_retains_intent_and_no_replace(store):
    client = Client((uri(1),))
    client.failure = "bad_readback"
    with pytest.raises(SpotifyReconciliationError, match="readback"):
        apply(client, store, (uri(9),))
    assert not client.writes
    client.failure = ""
    assert apply(client, store, (uri(9),)).status == "pending"
    assert len(client.additions) == 1
