from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist import engine_runtime
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import OperationalStatus
from news_bulletin_playlist.persistence import DEFAULT_DB_FILENAME
from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetrics


class _Auth:
    def __init__(self) -> None:
        self.metrics: SpotifyRequestMetrics | None = None

    def set_request_metrics(self, metrics: SpotifyRequestMetrics) -> None:
        self.metrics = metrics


def test_configured_scheduler_reconciles_only_after_runtime_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / DEFAULT_DB_FILENAME
    observed_at_reconcile: list[str] = []
    auth = _Auth()

    class _Lifecycle:
        def __init__(
            self,
            runner: object,
            *,
            interval: timedelta,
            diagnostics: object | None = None,
        ) -> None:
            del runner, interval, diagnostics
            self.status = OperationalStatus(configured=False)

        def reconcile(self, *, configured: bool) -> None:
            assert configured
            store = DiagnosticEventStore(database_path)
            store.initialize()
            observed_at_reconcile.extend(
                event.event_name for event in reversed(store.list_events(limit=10))
            )

        def wake(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

    class _Handler(engine_runtime.OperationalHealthHandler):
        pass

    def build_auth(
        _data_dir: Path,
        *,
        environ: object,
    ) -> tuple[None, _Auth]:
        del environ
        return None, auth

    monkeypatch.setattr(engine_runtime, "build_engine_runtime_auth", build_auth)
    monkeypatch.setattr(engine_runtime, "_load_runtime_config", lambda _data_dir, _env: object())
    monkeypatch.setattr(
        engine_runtime,
        "_build_managed_admin_service",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(engine_runtime, "EngineLifecycleController", _Lifecycle)

    stop = threading.Event()
    stop.set()

    assert (
        engine_runtime.serve(
            host="127.0.0.1",
            port=0,
            data_dir=tmp_path,
            stop_event=stop,
            environ={},
            handler_type=_Handler,
        )
        == 0
    )

    assert auth.metrics is not None
    assert observed_at_reconcile == ["runtime_ready"]
