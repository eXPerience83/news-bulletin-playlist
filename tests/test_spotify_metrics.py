from __future__ import annotations

import io
import json
import sqlite3
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.diagnostics_web import (
    DiagnosticFilters,
    build_diagnostic_bundle,
    render_diagnostics_page,
)
from news_bulletin_playlist.engine_runtime import _build_managed_admin_service
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.spotify.auth import (
    PRODUCTION_SCOPES,
    CredentialStatus,
    SpotifyAccountsClient,
    SpotifyAuthService,
    SpotifyCredentialRecord,
    SpotifyCredentialStore,
)
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)
from news_bulletin_playlist.spotify_backoff import (
    SpotifyRateLimitGuardClient,
    SpotifyRateLimitJournal,
    SpotifyRateLimitSuppressed,
)
from news_bulletin_playlist.spotify_metrics import (
    SpotifyOAuthOperation,
    SpotifyPublicOperation,
    SpotifyQuotaReason,
    SpotifyRequestMetrics,
    SpotifyRequestOutcome,
    SpotifyWebApiOperation,
)

NOW = datetime(2026, 9, 7, 10, 30, 45, tzinfo=UTC)
TOKEN = "access-token-sentinel"
BODY_SENTINEL = "provider-message-sentinel"


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class _ExecuteFailureContext:
    def __init__(self, context: object) -> None:
        self.context = context

    def __enter__(self):  # type: ignore[no-untyped-def]
        connection = self.context.__enter__()  # type: ignore[attr-defined]

        class ConnectionProxy:
            def execute(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                raise sqlite3.OperationalError("simulated metrics execute failure")

            def __getattr__(self, name: str) -> object:
                return getattr(connection, name)

        return ConnectionProxy()

    def __exit__(self, *args: object) -> object:
        return self.context.__exit__(*args)  # type: ignore[attr-defined]


def _metrics(tmp_path: Path) -> SpotifyRequestMetrics:
    metrics = SpotifyRequestMetrics(tmp_path / "state.sqlite3", clock=lambda: NOW)
    metrics.initialize()
    return metrics


def _metric(snapshot, *, operation: str, outcome: SpotifyRequestOutcome):  # type: ignore[no-untyped-def]
    return next(
        metric
        for metric in snapshot.metrics
        if metric.operation == operation and metric.outcome is outcome
    )


def test_web_api_counts_only_real_http_attempts_and_keeps_operations_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = _metrics(tmp_path)
    calls = 0

    def urlopen(request: object, *, timeout: float) -> _Response:
        nonlocal calls
        assert isinstance(request, urllib.request.Request)
        assert timeout == 30.0
        calls += 1
        return _Response(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = SpotifyClient(TOKEN, metrics=metrics)
    client.show_episodes("show")
    client.show_episodes("show", offset=50)
    client.playlist_items("playlist")
    client.playlist_snapshot("playlist")
    client.replace_playlist_items("playlist", ["spotify:episode:0123456789ABCDEFGHIJKL"])
    with pytest.raises(ValueError):
        client.replace_playlist_items("playlist", ["spotify:episode:bad"])

    snapshot = metrics.last_24_hours(now=NOW)
    assert calls == 5
    assert (
        _metric(
            snapshot,
            operation="show_episodes",
            outcome=SpotifyRequestOutcome.SUCCESS,
        ).request_count
        == 2
    )
    assert (
        _metric(
            snapshot,
            operation="playlist_items",
            outcome=SpotifyRequestOutcome.SUCCESS,
        ).request_count
        == 1
    )
    assert (
        _metric(
            snapshot,
            operation="playlist_snapshot",
            outcome=SpotifyRequestOutcome.SUCCESS,
        ).request_count
        == 1
    )
    assert (
        _metric(
            snapshot,
            operation="replace_playlist_items",
            outcome=SpotifyRequestOutcome.SUCCESS,
        ).request_count
        == 1
    )
    assert snapshot.web_api_requests == 5


def test_http_429_preserves_only_allowlisted_quota_reason_and_secrets_do_not_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    metrics = _metrics(tmp_path)

    def urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "ignored",
            {"Retry-After": "41164", "Authorization": TOKEN},
            io.BytesIO(
                (
                    '{"error":{"status":429,"reason":"QUOTA_EXCEEDED",'
                    f'"message":"{BODY_SENTINEL}","extra":"{TOKEN}"}}}}'
                ).encode()
            ),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(SpotifyApiError) as raised:
        SpotifyClient(TOKEN, metrics=metrics).search_shows("query")

    assert raised.value.retry_after == 41164
    assert raised.value.quota_reason is SpotifyQuotaReason.QUOTA_EXCEEDED
    exposed = str(raised.value) + capsys.readouterr().out + capsys.readouterr().err
    assert TOKEN not in exposed and BODY_SENTINEL not in exposed
    snapshot = metrics.last_24_hours(now=NOW)
    metric = _metric(snapshot, operation="search_shows", outcome=SpotifyRequestOutcome.HTTP_ERROR)
    assert metric.http_status == 429
    assert metric.quota_reason is SpotifyQuotaReason.QUOTA_EXCEEDED
    assert snapshot.http_rate_limits == 1
    assert snapshot.quota_exceeded == 1
    stored = metrics.path.read_bytes()
    assert TOKEN.encode() not in stored and BODY_SENTINEL.encode() not in stored


def test_quota_exceeded_reason_is_not_accepted_for_non_429(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _metrics(tmp_path)

    def urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        raise urllib.error.HTTPError(
            request.full_url,
            500,
            "ignored",
            {},
            io.BytesIO(b'{"error":{"reason":"QUOTA_EXCEEDED"}}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(SpotifyApiError) as raised:
        SpotifyClient(TOKEN, metrics=metrics).search_shows("query")

    assert raised.value.quota_reason is None
    metric = _metric(
        metrics.last_24_hours(now=NOW),
        operation="search_shows",
        outcome=SpotifyRequestOutcome.HTTP_ERROR,
    )
    assert metric.http_status == 500
    assert metric.quota_reason is None


@pytest.mark.parametrize("body", [b"not-json", b'{"error":{"reason":"OTHER"}}', b"x" * 9000])
def test_unknown_or_oversized_429_reason_remains_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    metrics = _metrics(tmp_path)

    def urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "ignored",
            {"Retry-After": "12"},
            io.BytesIO(body),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(SpotifyApiError) as raised:
        SpotifyClient(TOKEN, metrics=metrics).search_shows("query")
    assert raised.value.status == 429
    assert raised.value.retry_after == 12
    assert raised.value.quota_reason is None
    assert (
        _metric(
            metrics.last_24_hours(now=NOW),
            operation="search_shows",
            outcome=SpotifyRequestOutcome.HTTP_ERROR,
        ).quota_reason
        is None
    )


def test_http_error_body_read_is_limited_to_eight_kib(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _metrics(tmp_path)
    reads: list[int] = []

    class SizedBody(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            return super().read(size)

    def urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "ignored",
            {},
            SizedBody(b"x" * 9000),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(SpotifyApiError):
        SpotifyClient(TOKEN, metrics=metrics).search_shows("query")

    assert reads == [8 * 1024]


def test_sqlite_execute_failure_does_not_change_successful_spotify_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _metrics(tmp_path)
    original_connection = metrics._connection

    def failing_connection(operation: str) -> _ExecuteFailureContext:
        return _ExecuteFailureContext(original_connection(operation))

    def urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        assert timeout == 30.0
        return _Response(b'{"items":[]}')

    monkeypatch.setattr(metrics, "_connection", failing_connection)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert SpotifyClient(TOKEN, metrics=metrics).show_episodes("show") == {"items": []}

    monkeypatch.setattr(metrics, "_connection", original_connection)
    assert metrics.last_24_hours(now=NOW).coverage_complete is False


def test_sqlite_execute_failure_does_not_mask_429_or_prevent_rate_limit_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _metrics(tmp_path)
    original_connection = metrics._connection
    journal = SpotifyRateLimitJournal(SQLiteStore(metrics.path))

    def failing_connection(operation: str) -> _ExecuteFailureContext:
        return _ExecuteFailureContext(original_connection(operation))

    def urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "ignored",
            {"Retry-After": "41164"},
            io.BytesIO(b'{"error":{"reason":"QUOTA_EXCEEDED"}}'),
        )

    monkeypatch.setattr(metrics, "_connection", failing_connection)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    guarded = SpotifyRateLimitGuardClient(
        SpotifyClient(TOKEN, metrics=metrics),
        journal,
        clock=lambda: NOW,
    )

    with pytest.raises(SpotifyApiError) as raised:
        guarded.show_episodes("show")

    assert raised.value.status == 429
    assert raised.value.retry_after == 41164
    assert raised.value.quota_reason is SpotifyQuotaReason.QUOTA_EXCEEDED
    state = journal.active(now=NOW)
    assert state is not None
    assert state.retry_after_seconds == 41164


def test_sqlite_select_failure_returns_incomplete_metrics_without_exposing_sqlite_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _metrics(tmp_path)
    original_connection = metrics._connection

    def failing_connection(operation: str) -> _ExecuteFailureContext:
        return _ExecuteFailureContext(original_connection(operation))

    monkeypatch.setattr(metrics, "_connection", failing_connection)
    snapshot = metrics.last_24_hours(now=NOW)
    page = render_diagnostics_page(
        events=(),
        filters=DiagnosticFilters(),
        spotify_metrics=snapshot,
    )
    bundle = build_diagnostic_bundle(
        events=(),
        generated_at=NOW,
        last_cycle=None,
        retention_days=30,
        max_events=10_000,
        spotify_metrics=snapshot,
    )

    assert snapshot.coverage_complete is False
    assert b"Coverage: incomplete" in page
    assert b"Web API requests: 0" not in page
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        runtime = json.loads(archive.read("runtime.json"))
    assert runtime["spotify_request_metrics_last_24_hours"]["web_api_requests"] is None


def test_transport_error_is_counted_after_real_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _metrics(tmp_path)

    def urlopen(request: object, *, timeout: float) -> _Response:
        del request, timeout
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(SpotifyTransportError):
        SpotifyClient(TOKEN, metrics=metrics).playlist_items("playlist")
    assert (
        _metric(
            metrics.last_24_hours(now=NOW),
            operation="playlist_items",
            outcome=SpotifyRequestOutcome.TRANSPORT_ERROR,
        ).request_count
        == 1
    )


def test_metrics_survive_restart_prune_and_fail_soft(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path)
    metrics.record_public(
        SpotifyPublicOperation.EMBED_SHOW,
        SpotifyRequestOutcome.SUCCESS,
        http_status=200,
        occurred_at=NOW - timedelta(hours=2),
    )
    metrics.record_web_api(
        SpotifyWebApiOperation.SHOW_EPISODES,
        SpotifyRequestOutcome.SUCCESS,
        occurred_at=NOW - timedelta(hours=23),
    )
    metrics.record_oauth(
        SpotifyOAuthOperation.TOKEN_REFRESH,
        SpotifyRequestOutcome.SUCCESS,
        occurred_at=NOW,
    )
    metrics.record_web_api(
        SpotifyWebApiOperation.SEARCH_SHOWS,
        SpotifyRequestOutcome.SUCCESS,
        occurred_at=NOW - timedelta(days=31),
    )
    restarted = SpotifyRequestMetrics(metrics.path)
    snapshot = restarted.last_24_hours(now=NOW)
    assert snapshot.public_requests == 1
    assert snapshot.web_api_requests == 1
    assert snapshot.oauth_requests == 1
    assert snapshot.oauth_refreshes == 1

    broken = SpotifyRequestMetrics(tmp_path / "not-a-directory" / "state.sqlite3")
    (tmp_path / "not-a-directory").write_text("blocked")
    broken.record_web_api(SpotifyWebApiOperation.SHOW_EPISODES, SpotifyRequestOutcome.SUCCESS)
    assert broken.last_24_hours(now=NOW).coverage_complete is False


def test_oauth_refresh_counts_real_transport_but_cached_access_token_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = _metrics(tmp_path)

    def urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        return _Response(
            b'{"access_token":"new-token","token_type":"Bearer","expires_in":3600,'
            b'"scope":"playlist-read-private playlist-modify-private playlist-modify-public"}'
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    credentials = SpotifyCredentialStore(tmp_path / "spotify-auth.json")
    credentials.save(
        SpotifyCredentialRecord(
            status=CredentialStatus.ACTIVE,
            refresh_token="refresh-token-sentinel",
            granted_scopes=PRODUCTION_SCOPES,
            authorized_at=NOW,
        )
    )
    service = SpotifyAuthService(
        client_id="client-id",
        redirect_uri="https://news.example.test/admin/spotify/callback",
        store=credentials,
        transport=SpotifyAccountsClient(metrics=metrics),
    )
    assert service.get_access_token(now=NOW) == "new-token"
    assert service.get_access_token(now=NOW + timedelta(minutes=1)) == "new-token"
    snapshot = metrics.last_24_hours(now=NOW)
    assert (
        _metric(
            snapshot,
            operation="token_refresh",
            outcome=SpotifyRequestOutcome.SUCCESS,
        ).request_count
        == 1
    )
    assert snapshot.oauth_refreshes == 1


def test_backoff_suppression_before_transport_counts_zero_web_api_requests(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path)
    journal = SpotifyRateLimitJournal(SQLiteStore(metrics.path))
    journal.activate(observed_at=NOW, retry_after_seconds=60)
    client = SpotifyRateLimitGuardClient(
        SpotifyClient(TOKEN, metrics=metrics),
        journal,
        clock=lambda: NOW + timedelta(seconds=1),
    )

    with pytest.raises(SpotifyRateLimitSuppressed):
        client.show_episodes("show")

    assert metrics.last_24_hours(now=NOW).web_api_requests == 0


def test_runtime_managed_admin_client_records_real_web_api_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = _metrics(tmp_path)
    service = _build_managed_admin_service(
        data_dir=tmp_path,
        spotify_auth=object(),  # type: ignore[arg-type]
        request_metrics=metrics,
    )
    assert service is not None

    def urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        assert timeout == 30.0
        return _Response(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    service.client_factory(TOKEN).create_playlist("Test playlist")

    metric = _metric(
        metrics.last_24_hours(now=NOW),
        operation="create_playlist",
        outcome=SpotifyRequestOutcome.SUCCESS,
    )
    assert metric.request_count == 1


def test_metrics_are_sanitized_in_diagnostics_page_and_every_zip_file(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path)
    metrics.record_public(
        SpotifyPublicOperation.EMBED_SHOW,
        SpotifyRequestOutcome.SUCCESS,
        http_status=200,
        occurred_at=NOW,
    )
    metrics.record_oauth(
        SpotifyOAuthOperation.TOKEN_REFRESH,
        SpotifyRequestOutcome.SUCCESS,
        occurred_at=NOW,
    )
    metrics.record_web_api(
        SpotifyWebApiOperation.REPLACE_PLAYLIST_ITEMS,
        SpotifyRequestOutcome.HTTP_ERROR,
        http_status=502,
        occurred_at=NOW,
    )
    snapshot = metrics.last_24_hours(now=NOW)
    page = render_diagnostics_page(
        events=(),
        filters=DiagnosticFilters(),
        spotify_metrics=snapshot,
    )
    bundle = build_diagnostic_bundle(
        events=(),
        generated_at=NOW,
        last_cycle=None,
        retention_days=30,
        max_events=10_000,
        spotify_metrics=snapshot,
    )
    assert TOKEN.encode() not in page and BODY_SENTINEL.encode() not in page
    assert b"Public Spotify requests (no OAuth): 1" in page
    assert b"Spotify Web API requests (authenticated): 1" in page
    assert b"OAuth token requests: 1" in page
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        for name in archive.namelist():
            contents = archive.read(name)
            assert TOKEN.encode() not in contents, name
            assert BODY_SENTINEL.encode() not in contents, name
        runtime = json.loads(archive.read("runtime.json"))
    metric_document = runtime["spotify_request_metrics_last_24_hours"]
    assert metric_document["public_requests"] == 1
    assert metric_document["web_api_requests"] == 1
    assert metric_document["oauth_requests"] == 1
    assert metric_document["oauth_refreshes"] == 1
    assert metric_document["http_429s"] == 0
    assert metric_document["breakdown"] == [
        {
            "http_status": None,
            "operation": "token_refresh",
            "outcome": "success",
            "quota_reason": None,
            "request_count": 1,
            "surface": "oauth",
        },
        {
            "http_status": 200,
            "operation": "embed_show",
            "outcome": "success",
            "quota_reason": None,
            "request_count": 1,
            "surface": "public",
        },
        {
            "http_status": 502,
            "operation": "replace_playlist_items",
            "outcome": "http_error",
            "quota_reason": None,
            "request_count": 1,
            "surface": "web_api",
        },
    ]


def test_metric_persistence_failure_does_not_change_successful_spotify_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = _metrics(tmp_path)

    def urlopen(request: object, *, timeout: float) -> _Response:
        assert isinstance(request, urllib.request.Request)
        assert timeout == 30.0
        return _Response(b'{"items":[]}')

    def unavailable(operation: str) -> object:
        raise PersistenceError(f"{operation} unavailable")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(metrics, "_connection", unavailable)
    assert SpotifyClient(TOKEN, metrics=metrics).show_episodes("show") == {"items": []}
    snapshot = metrics.last_24_hours(now=NOW)
    page = render_diagnostics_page(
        events=(),
        filters=DiagnosticFilters(),
        spotify_metrics=snapshot,
    )
    assert b"Coverage: incomplete" in page
    assert b"Web API requests: 0" not in page


@pytest.mark.parametrize(
    ("gap_age", "expected_complete"),
    [
        (timedelta(hours=1), False),
        (timedelta(hours=23, minutes=59), False),
        (timedelta(hours=23, minutes=59, seconds=59), False),
        (timedelta(hours=24), False),
        (timedelta(hours=24, seconds=1), True),
    ],
)
def test_coverage_is_evaluated_against_the_requested_window_in_the_same_process(
    tmp_path: Path,
    gap_age: timedelta,
    expected_complete: bool,
) -> None:
    metrics = _metrics(tmp_path)
    metrics._mark_incomplete(NOW - gap_age)

    assert metrics.last_24_hours(now=NOW).coverage_complete is expected_complete


@pytest.mark.parametrize(
    ("gap_age", "expected_complete"),
    [
        (timedelta(hours=23, minutes=59, seconds=59), False),
        (timedelta(hours=24), False),
        (timedelta(hours=24, seconds=1), True),
    ],
)
def test_coverage_window_decision_survives_restart(
    tmp_path: Path,
    gap_age: timedelta,
    expected_complete: bool,
) -> None:
    metrics = _metrics(tmp_path)
    metrics._mark_incomplete(NOW - gap_age)

    restarted = SpotifyRequestMetrics(metrics.path, clock=lambda: NOW)
    restarted.initialize()
    snapshot = restarted.last_24_hours(now=NOW)
    bundle = build_diagnostic_bundle(
        events=(),
        generated_at=NOW,
        last_cycle=None,
        retention_days=30,
        max_events=10_000,
        spotify_metrics=snapshot,
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        runtime = json.loads(archive.read("runtime.json"))
    metric_document = runtime["spotify_request_metrics_last_24_hours"]
    assert snapshot.coverage_complete is expected_complete
    if expected_complete:
        assert metric_document["public_requests"] == 0
        assert metric_document["web_api_requests"] == 0
        assert metric_document["oauth_requests"] == 0
        assert metric_document["recorded_counts"] is None
    else:
        assert metric_document["public_requests"] is None
        assert metric_document["web_api_requests"] is None
        assert metric_document["oauth_requests"] is None
        assert metric_document["recorded_counts"] == {
            "http_429s": 0,
            "oauth_refreshes": 0,
            "oauth_requests": 0,
            "public_requests": 0,
            "quota_exceeded": 0,
            "web_api_requests": 0,
        }


def test_recovered_sqlite_persists_a_pending_gap_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _metrics(tmp_path)
    original_connection = metrics._connection
    attempts = 0

    def fail_first_two_connections(operation: str):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PersistenceError(f"{operation} unavailable")
        return original_connection(operation)

    monkeypatch.setattr(metrics, "_connection", fail_first_two_connections)
    gap_at = NOW - timedelta(minutes=5)
    metrics.record_web_api(
        SpotifyWebApiOperation.SHOW_EPISODES,
        SpotifyRequestOutcome.SUCCESS,
        occurred_at=gap_at,
    )
    metrics.record_web_api(
        SpotifyWebApiOperation.SHOW_EPISODES,
        SpotifyRequestOutcome.SUCCESS,
        occurred_at=NOW,
    )

    restarted = SpotifyRequestMetrics(metrics.path, clock=lambda: NOW)
    restarted.initialize()
    snapshot = restarted.last_24_hours(now=NOW)
    assert snapshot.coverage_complete is False
    assert snapshot.web_api_requests == 1
