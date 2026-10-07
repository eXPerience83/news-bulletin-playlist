"""Null slots are positive partial evidence, never source authority."""

from __future__ import annotations

import io
import json
import logging
import sqlite3
import zipfile
from dataclasses import replace
from datetime import timedelta

import pytest
from _spotify_catalogue import catalogue_page
from test_catalogue_validation_diagnostics import (
    NOW,
    SOURCE,
    _Auth,
    _Client,
    _item,
    _safe,
)

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics import DiagnosticEventStore, DiagnosticSeverity
from news_bulletin_playlist.diagnostics_web import build_diagnostic_bundle
from news_bulletin_playlist.managed_state import ManagedState
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.source_inspection import (
    inspect_configured_source,
    serialize_source_inspection,
)
from news_bulletin_playlist.spotify.catalogue_errors import CatalogueValidationReason
from news_bulletin_playlist.spotify.client import SpotifyApiError
from news_bulletin_playlist.spotify.first_catalogue import (
    CatalogueResponseError,
    apply_public_release_evidence,
    fetch_source_catalogue,
)
from news_bulletin_playlist.spotify.public_detector import PublicReleaseDateShape, spotify_show_id
from news_bulletin_playlist.spotify.public_release import PublicReleaseEvidence
from news_bulletin_playlist.spotify_backoff import (
    SpotifyRateLimitGuardClient,
    SpotifyRateLimitJournal,
    SpotifyRateLimitSuppressed,
)


def pages(total, nulls=()):
    result = []
    for offset in range(0, total, 50):
        items = [None if i in nulls else _item(i) for i in range(offset, min(offset + 50, total))]
        result.append(
            catalogue_page(
                items,
                offset=offset,
                total=total,
                next_url="next" if offset + len(items) < total else None,
            )
        )
    return result


def fetch(client, *, source=SOURCE):
    return fetch_source_catalogue(
        client, source, now=NOW, cutoff=NOW - timedelta(days=2), collect_evidence=True
    )


@pytest.mark.parametrize(
    "total,nulls",
    [
        (4, (0,)),
        (4, (1,)),
        (50, (49,)),
        (51, (50,)),
        (97, (96,)),
        (100, (99,)),
        (100, (1, 2, 70)),
        (200, (96,)),
        (250, (96,)),
        (250, (199,)),
        (250, (220,)),
        (1, (0,)),
        (50, tuple(range(50))),
        (200, tuple(range(200))),
        (250, tuple(range(200))),
    ],
)
def test_null_locations_bounds_and_original_provider_positions(total, nulls):
    client = _Client(pages(total, nulls))
    snapshot = fetch(client)
    observed = min(total, 200)
    expected_nulls = sum(i < observed for i in nulls)
    assert snapshot.null_item_count == expected_nulls
    assert not snapshot.complete and not snapshot.coverage_proven
    assert snapshot.api_latest_id is None
    assert snapshot.request_count == min((total + 49) // 50, 4)
    assert client.calls == [("episodes", i * 50) for i in range(snapshot.request_count)]
    positions = [i for i in range(observed) if i not in nulls]
    assert snapshot.api_ids == tuple(f"{i:022d}" for i in positions)
    assert [row.position for row in snapshot.evidence] == [i + 1 for i in positions]
    assert len(snapshot.episodes) == observed - expected_nulls


@pytest.mark.parametrize(
    "dates,null_index",
    [
        (["2026-10-07", "2026-09-01"], 0),
        (["2026-10-07", "2026-09-01"], 1),
        (["2026-09-01", "2026-08-01"], 2),
        (["2026-10-07", "2026-10-06"], 1),
        (["2026-10-07", "2026-09-01", "2026-08-01"], 3),
    ],
)
def test_null_never_uses_known_boundary_to_rescue_coverage(dates, null_index):
    items = [dict(_item(i), release_date=day) for i, day in enumerate(dates)]
    items.insert(null_index, None)
    # >200 total leaves boundary proof as the only ordinary route to coverage.
    snapshot = fetch_source_catalogue(
        _Client([catalogue_page(items, total=250, next_url="next")]),
        SOURCE,
        now=NOW,
        cutoff=NOW - timedelta(days=6),
        max_pages=1,
    )
    assert not snapshot.complete and not snapshot.coverage_proven
    assert snapshot.api_latest_id is None


@pytest.mark.parametrize("kind", ["duplicate", "total_changed", "rate_limit"])
def test_error_after_null_still_fails_closed(tmp_path, kind):
    responses = pages(151, (96,))
    if kind == "duplicate":
        responses[2]["items"][5] = _item(0)
    elif kind == "total_changed":
        responses[2]["total"] = 152

    class Client(_Client):
        def show_episodes(self, show_id, *, limit=50, offset=0):
            if offset == 100 and kind == "rate_limit":
                self.calls.append(("episodes", offset))
                raise SpotifyApiError(429, "secret-provider-body", retry_after=1800)
            return super().show_episodes(show_id, limit=limit, offset=offset)

    client = Client(responses)
    journal = SpotifyRateLimitJournal(SQLiteStore(tmp_path / "state.sqlite3"))
    guarded = SpotifyRateLimitGuardClient(client, journal, clock=lambda: NOW)
    if kind == "rate_limit":
        with pytest.raises(SpotifyApiError) as raised:
            fetch(guarded)
        assert raised.value.status == 429
        state = journal.active(now=NOW)
        assert state is not None and state.retry_not_before == NOW + timedelta(seconds=1800)
        with pytest.raises(SpotifyRateLimitSuppressed):
            guarded.show_episodes(spotify_show_id(SOURCE))
    else:
        with pytest.raises(CatalogueResponseError) as raised:
            fetch(guarded)
        assert raised.value.reason is CatalogueValidationReason(
            "duplicate_episode_id" if kind == "duplicate" else "total_changed"
        )
        assert raised.value.requested_offset == 100
    assert client.calls == [("episodes", 0), ("episodes", 50), ("episodes", 100)]


@pytest.mark.parametrize("nulls", [(), (0,), (96,)])
def test_public_enrichment_cannot_restore_latest_id(nulls):
    responses = pages(97, nulls)
    # Distinct chronology proves one visible latest ID when no null is present.
    for page in responses:
        for item in page["items"]:
            if item is not None:
                item["release_date"] = "2026-10-07" if item["id"] == f"{1:022d}" else "2026-09-01"
    snapshot = fetch(_Client(responses))
    evidence = PublicReleaseEvidence(
        SOURCE.id,
        spotify_show_id(SOURCE),
        f"{1:022d}",
        "2026-10-07T08:00:00Z",
        PublicReleaseDateShape.OBJECT_ISO_STRING,
        NOW,
    )
    enriched = apply_public_release_evidence(snapshot, SOURCE, evidence)
    assert enriched.null_item_count == snapshot.null_item_count
    assert enriched.api_latest_id == (None if nulls else f"{1:022d}")
    assert enriched.complete == snapshot.complete
    assert enriched.coverage_proven == snapshot.coverage_proven


def test_inspection_null_success_diagnostics_export_privacy_and_read_only(tmp_path, caplog):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyRateLimitJournal(store)
    diagnostic_store = DiagnosticEventStore(store.path)
    logger = logging.getLogger("null-slot-tests")
    caplog.set_level(logging.WARNING, logger=logger.name)
    journal.diagnostics = OperationalDiagnostics(diagnostic_store, logger=logger)
    client = _Client(pages(100, (96,)))
    # The entire authenticated body/extra fields must stay out of diagnostics/report.
    for page in client.pages:
        page["secret"] = "secret-provider-body"
    with sqlite3.connect(store.path) as connection:
        before = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if table not in {"diagnostic_events", "sqlite_sequence"}
        }
    report = inspect_configured_source(
        SOURCE.id,
        catalog=BUILTIN_CATALOG,
        state=ManagedState(),
        auth=_Auth(),
        rate_limit=journal,
        now=NOW,
        client_factory=lambda token: client,
        observe_public=False,
    )
    assert report.null_item_count == 1
    assert not report.catalogue_complete and not report.coverage_proven
    assert "catalogue_null_slots_observed" in report.warnings
    assert "coverage_unproven" in report.warnings
    assert report.episodes[-1].catalogue_position == 100
    serialized = serialize_source_inspection(report)
    assert json.loads(serialized)["null_item_count"] == 1
    assert serialized == serialize_source_inspection(report)
    assert b"secret-provider-body" not in serialized and b"secret-token" not in serialized
    with sqlite3.connect(store.path) as connection:
        for table, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{table}"').fetchall() == rows
        for table in (
            "spotify_first_episode_state",
            "spotify_first_episode_history",
            "spotify_first_source_state",
        ):
            exists = connection.execute(
                "SELECT name FROM sqlite_master WHERE name=?", (table,)
            ).fetchone()
            assert exists is None or not connection.execute(f'SELECT * FROM "{table}"').fetchall()
        raw = str(connection.execute("SELECT * FROM diagnostic_events").fetchall())
    events = diagnostic_store.list_events()
    assert len(events) == 1
    assert events[0].event_name == "source_catalogue_null_slots_observed"
    assert events[0].details == {
        "null_item_count": 1,
        "catalogue_complete": False,
        "coverage_proven": False,
    }
    _safe(str(events) + raw + caplog.text)
    bundle = build_diagnostic_bundle(
        events=events,
        generated_at=NOW,
        last_cycle=None,
        retention_days=30,
        max_events=100,
        build_revision="test-build",
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        for name in ("diagnostics.jsonl", "diagnostics.txt"):
            text = archive.read(name).decode()
            _safe(text)
            assert "null_item_count" in text


@pytest.mark.parametrize("value", [-1, 201, None, True, "secret-provider-body", 1.5])
def test_diagnostics_null_count_allowlist_is_bounded(tmp_path, value):
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    with pytest.raises(ValueError):
        store.record(
            occurred_at=NOW,
            severity=DiagnosticSeverity.WARNING,
            component="catalogue",
            event_name="source_catalogue_null_slots_observed",
            details={"null_item_count": value},
        )


@pytest.mark.parametrize("count", [-1, 201, True, None, "x"])
def test_snapshot_rejects_invalid_null_metadata(count):
    clean = fetch(_Client(pages(1)))
    with pytest.raises(ValueError):
        replace(clean, null_item_count=count)


def test_snapshot_null_metadata_cannot_certify_content():
    clean = fetch(_Client(pages(2)))
    for changes in ({"complete": True}, {"coverage_proven": True}, {"api_latest_id": "A" * 22}):
        with pytest.raises(ValueError):
            replace(
                clean,
                **dict(
                    null_item_count=1,
                    api_ids=clean.api_ids[:1],
                    complete=False,
                    coverage_proven=False,
                    api_latest_id=None,
                )
                | changes,
            )


@pytest.mark.parametrize("limited", [False, True])
def test_null_continuation_request_metrics_and_global_429(tmp_path, monkeypatch, limited):
    import urllib.error
    import urllib.parse

    from test_spotify_metrics import _Response

    from news_bulletin_playlist.spotify.client import SpotifyClient
    from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetrics, SpotifyRequestOutcome

    metrics = SpotifyRequestMetrics(tmp_path / "state.sqlite3", clock=lambda: NOW)
    metrics.initialize()
    responses = pages(250, (96,))
    calls = []

    def urlopen(request, *, timeout):
        assert request.get_method() == "GET"
        assert urllib.parse.urlsplit(request.full_url).path.endswith("/episodes")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        offset = int(query["offset"][0])
        assert query["limit"] == ["50"]
        calls.append(offset)
        if limited and offset == 100:
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "rate limited",
                {"Retry-After": "1800"},
                io.BytesIO(b'{"error":{"message":"secret-provider-body"}}'),
            )
        return _Response(json.dumps(responses[offset // 50]).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    journal = SpotifyRateLimitJournal(SQLiteStore(metrics.path))
    client = SpotifyRateLimitGuardClient(
        SpotifyClient("secret-token", metrics=metrics), journal, clock=lambda: NOW
    )
    if limited:
        with pytest.raises(SpotifyApiError):
            fetch(client)
        with pytest.raises(SpotifyRateLimitSuppressed):
            fetch(client)
        expected_count = 3
    else:
        snapshot = fetch(client)
        assert snapshot.request_count == 4 and snapshot.null_item_count == 1
        expected_count = 4
    assert calls == [50 * i for i in range(expected_count)]
    measured = metrics.last_24_hours(now=NOW)
    assert measured.web_api_requests == expected_count and measured.oauth_requests == 0
    assert all(row.operation == "show_episodes" for row in measured.metrics)
    errors = sum(
        row.request_count
        for row in measured.metrics
        if row.outcome is SpotifyRequestOutcome.HTTP_ERROR
    )
    assert errors == int(limited)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("id", None, "invalid_episode_id"),
        ("uri", "secret-uri", "invalid_episode_uri"),
        ("uri", "spotify:episode:" + "Z" * 22, "episode_uri_id_mismatch"),
        ("name", " ", "invalid_title"),
        ("release_date", "2026-02-30", "invalid_release_date"),
        ("release_date_precision", "hour", "invalid_release_precision"),
        ("duration_ms", -1, "invalid_duration"),
        ("duration_ms", True, "invalid_duration"),
    ],
)
def test_null_does_not_relax_later_object_validation(field, value, reason):
    item = dict(_item(1), **{field: value})
    with pytest.raises(CatalogueResponseError) as raised:
        fetch(_Client([catalogue_page([None, item])]))
    assert raised.value.reason is CatalogueValidationReason(reason)
    assert raised.value.requested_offset == 0 and raised.value.item_index == 1
    assert raised.value.item_value_kind is None


def test_null_does_not_consult_boundary_or_latest_proofs(monkeypatch):
    import news_bulletin_playlist.spotify.first_catalogue as catalogue

    def forbidden(*args, **kwargs):
        raise AssertionError("unknown slot cannot be certified using only visible episodes")

    monkeypatch.setattr(catalogue, "_boundary_covers", forbidden)
    monkeypatch.setattr(catalogue, "_provable_latest_id", forbidden)
    snapshot = fetch(_Client(pages(250, (96,))))
    evidence = PublicReleaseEvidence(
        SOURCE.id,
        spotify_show_id(SOURCE),
        f"{1:022d}",
        "2026-10-07T08:00:00Z",
        PublicReleaseDateShape.OBJECT_ISO_STRING,
        NOW,
    )
    enriched = apply_public_release_evidence(snapshot, SOURCE, evidence)
    assert enriched.api_latest_id is None and enriched.null_item_count == 1
