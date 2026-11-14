"""Command-line entrypoint for the application runtime."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from news_bulletin_playlist.diagnostics_runtime import serve
from news_bulletin_playlist.inspection_fence import (
    InspectionGateError,
    inspect_gate,
    release_gate,
)
from news_bulletin_playlist.runtime import (
    DEFAULT_DATA_DIR,
    DEFAULT_HEALTH_HOST,
    DEFAULT_HEALTH_PORT,
    DEFAULT_HEALTH_URL,
    healthcheck,
)
from news_bulletin_playlist.upgrade_inspection_report import (
    UpgradeInspectionError,
    upgrade_inspection_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="news-playlist")
    commands = parser.add_subparsers(dest="command", required=True)

    serve_parser = commands.add_parser("serve", help="run the long-lived application runtime")
    serve_parser.add_argument("--host", default=DEFAULT_HEALTH_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_HEALTH_PORT)
    serve_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    inspect_parser = commands.add_parser(
        "inspection-status", help="read v11 reconciliation hold state and exact SHA-256"
    )
    inspect_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    report_parser = commands.add_parser(
        "inspection-report",
        help="compare persisted v11 sources/playlists/policies with original legacy evidence",
    )
    report_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    release_parser = commands.add_parser(
        "inspection-release", help="explicitly release held Spotify reconciliation on next restart"
    )
    release_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    release_parser.add_argument("--state-sha256", required=True)
    release_parser.add_argument("--confirm", required=True)

    health_parser = commands.add_parser("healthcheck", help="check the local runtime health")
    health_parser.add_argument("--url", default=DEFAULT_HEALTH_URL)
    health_parser.add_argument("--timeout", type=float, default=2.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(host=args.host, port=args.port, data_dir=args.data_dir)
    if args.command == "healthcheck":
        return healthcheck(url=args.url, timeout=args.timeout)
    if args.command == "inspection-status":
        try:
            status = inspect_gate(args.data_dir)
        except InspectionGateError as exc:
            parser.error(str(exc))
        print(f"Spotify reconciliation gate: {status.state}")
        print(f"v11 state SHA-256: {status.state_sha256}")
        if status.original_release_sha256 is not None:
            print(f"Release acknowledged for: {status.original_release_sha256}")
        return 0
    if args.command == "inspection-report":
        try:
            report = upgrade_inspection_report(args.data_dir)
        except UpgradeInspectionError as exc:
            parser.error(str(exc))
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if report["safe_to_review_without_spotify_writes"] else 1
    if args.command == "inspection-release":
        try:
            status = release_gate(
                args.data_dir,
                expected_sha256=args.state_sha256,
                confirm=args.confirm,
            )
        except InspectionGateError as exc:
            parser.error(str(exc))
        print(
            f"Reconciliation release acknowledged for {status.state_sha256}. "
            "Stop inspection runtime and perform a normal restart without "
            "NEWS_PLAYLIST_STARTUP_INSPECTION=1."
        )
        return 0
    raise AssertionError(f"unhandled command: {args.command}")
