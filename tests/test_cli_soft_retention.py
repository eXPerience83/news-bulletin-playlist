from __future__ import annotations

from pathlib import Path

from news_bulletin_playlist import cli, engine_runtime
from news_bulletin_playlist.production_observability import (
    ProductionInstrumentedEngineCycleRunner,
)
from news_bulletin_playlist.soft_retention_engine import SoftRetentionEngineRunner


def test_serve_composes_soft_retention_and_production_observability_and_restores_runtime(
    monkeypatch,
) -> None:
    original_engine = engine_runtime.EngineRunner
    original_instrumented = engine_runtime.InstrumentedEngineCycleRunner
    observed: dict[str, object] = {}

    def fake_serve(*, host: str, port: int, data_dir: Path) -> int:
        observed["host"] = host
        observed["port"] = port
        observed["data_dir"] = data_dir
        observed["runner"] = vars(engine_runtime)["EngineRunner"]
        observed["instrumented_runner"] = vars(engine_runtime)["InstrumentedEngineCycleRunner"]
        return 17

    monkeypatch.setattr(cli, "serve", fake_serve)
    data_dir = Path("/tmp/soft-retention-test")

    result = cli._serve_with_soft_retention(
        host="127.0.0.1",
        port=8080,
        data_dir=data_dir,
    )

    assert result == 17
    assert observed == {
        "host": "127.0.0.1",
        "port": 8080,
        "data_dir": data_dir,
        "runner": SoftRetentionEngineRunner,
        "instrumented_runner": ProductionInstrumentedEngineCycleRunner,
    }
    assert vars(engine_runtime)["EngineRunner"] is original_engine
    assert vars(engine_runtime)["InstrumentedEngineCycleRunner"] is original_instrumented
