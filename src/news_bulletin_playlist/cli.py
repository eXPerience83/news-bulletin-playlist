"""Command-line entrypoint for the application runtime."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from news_bulletin_playlist import engine_runtime
from news_bulletin_playlist.diagnostics_runtime import serve
from news_bulletin_playlist.production_observability import (
    ProductionInstrumentedEngineCycleRunner,
)
from news_bulletin_playlist.runtime import (
    DEFAULT_DATA_DIR,
    DEFAULT_HEALTH_HOST,
    DEFAULT_HEALTH_PORT,
    DEFAULT_HEALTH_URL,
    healthcheck,
)
from news_bulletin_playlist.soft_retention_engine import SoftRetentionEngineRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="news-playlist")
    commands = parser.add_subparsers(dest="command", required=True)

    serve_parser = commands.add_parser("serve", help="run the long-lived application runtime")
    serve_parser.add_argument("--host", default=DEFAULT_HEALTH_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_HEALTH_PORT)
    serve_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    health_parser = commands.add_parser("healthcheck", help="check the local runtime health")
    health_parser.add_argument("--url", default=DEFAULT_HEALTH_URL)
    health_parser.add_argument("--timeout", type=float, default=2.0)
    return parser


def _serve_with_soft_retention(*, host: str, port: int, data_dir: Path) -> int:
    """Compose #181 plus production-only observability into the runtime."""
    runtime_namespace = vars(engine_runtime)
    original_engine_runner = runtime_namespace["EngineRunner"]
    original_instrumented_runner = runtime_namespace["InstrumentedEngineCycleRunner"]
    runtime_namespace["EngineRunner"] = SoftRetentionEngineRunner
    runtime_namespace["InstrumentedEngineCycleRunner"] = ProductionInstrumentedEngineCycleRunner
    try:
        return serve(host=host, port=port, data_dir=data_dir)
    finally:
        runtime_namespace["InstrumentedEngineCycleRunner"] = original_instrumented_runner
        runtime_namespace["EngineRunner"] = original_engine_runner


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _serve_with_soft_retention(host=args.host, port=args.port, data_dir=args.data_dir)
    if args.command == "healthcheck":
        return healthcheck(url=args.url, timeout=args.timeout)
    raise AssertionError(f"unhandled command: {args.command}")
