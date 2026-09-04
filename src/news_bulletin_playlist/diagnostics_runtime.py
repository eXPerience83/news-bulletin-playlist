"""Compose authenticated diagnostics into the production runtime without provider raw data."""

from __future__ import annotations

import html
import os
import threading
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from http import HTTPStatus
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from news_bulletin_playlist import __version__, engine_runtime
from news_bulletin_playlist.build_info import build_revision as resolve_build_revision
from news_bulletin_playlist.diagnostics import DiagnosticEvent
from news_bulletin_playlist.diagnostics_web import (
    DiagnosticFilters,
    build_diagnostic_bundle,
    parse_diagnostic_filters,
    render_diagnostics_page,
)
from news_bulletin_playlist.engine import EngineCycleResult, OperationalStatus
from news_bulletin_playlist.managed_admin import ManagedAdminError, ManagedAdminService
from news_bulletin_playlist.managed_admin_web import (
    max_duration_seconds_from_form,
    playlist_id_from_form,
    single_form_value,
)
from news_bulletin_playlist.persistence import PersistenceError
from news_bulletin_playlist.runtime import (
    DEFAULT_DATA_DIR,
    DEFAULT_HEALTH_HOST,
    DEFAULT_HEALTH_PORT,
    _data_dir_ready,
)
from news_bulletin_playlist.spotify.auth import AuthorizationState


class DiagnosticOperationalHealthHandler(engine_runtime.OperationalHealthHandler):
    """Add private diagnostic routes and keep detailed failures off the public page."""

    diagnostic_timezone: tzinfo = UTC
    diagnostic_timezone_label = "UTC"
    build_revision = "dev"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, b"Invalid request")
            return

        if parsed.path == "/":
            self._serve_safe_public_status()
            return
        if parsed.path == "/admin/diagnostics":
            self._serve_diagnostics(parsed.query)
            return
        if parsed.path == "/admin/diagnostics/export.zip":
            self._serve_diagnostics_export(parsed.query)
            return
        super().do_GET()

    def _activate_managed_playlist(
        self,
        service: ManagedAdminService,
        form: Mapping[str, list[str]],
    ) -> str:
        auth = self.managed_admin_auth
        if auth is None:
            raise ManagedAdminError("Spotify authorization is required to create a playlist")
        access_token = auth.get_access_token()
        managed = service.activate(
            template_id=single_form_value(form, "template_id"),
            display_name=single_form_value(form, "display_name"),
            description=single_form_value(form, "description", required=False),
            cover_id=single_form_value(form, "cover_id"),
            source_ids=form.get("source_id", []),
            access_token=access_token,
            max_duration_seconds=max_duration_seconds_from_form(form),
        )
        return str(managed.id)

    def _update_managed_playlist(
        self,
        service: ManagedAdminService,
        form: Mapping[str, list[str]],
    ) -> tuple[str, bool]:
        playlist_id = playlist_id_from_form(form)
        name = single_form_value(form, "display_name")
        description = single_form_value(form, "description", required=False)
        cover_id = single_form_value(form, "cover_id")
        enabled_values = form.get("enabled", [])
        if enabled_values not in ([], ["1"]):
            raise ValueError("enabled must be omitted or set exactly once")
        enabled = bool(enabled_values)

        snapshot = service.snapshot()
        current = next(
            (playlist for playlist in snapshot.managed if playlist.id == playlist_id),
            None,
        )
        if current is None:
            raise ManagedAdminError(f"unknown managed playlist: {playlist_id}")
        metadata_changed = (
            name.strip() != current.display_name or description != current.description
        )
        access_token: str | None = None
        if metadata_changed:
            auth = self.managed_admin_auth
            if auth is None:
                raise ManagedAdminError(
                    "Spotify must be connected to change playlist name or description"
                )
            access_token = auth.get_access_token()
        updated = service.update(
            playlist_id,
            display_name=name,
            description=description,
            cover_id=cover_id,
            source_ids=form.get("source_id", []),
            enabled=enabled,
            access_token=access_token,
            max_duration_seconds=max_duration_seconds_from_form(form),
        )
        return str(updated.id), updated.enabled

    def _serve_safe_public_status(self) -> None:
        ready = _data_dir_ready(self.data_dir)
        payload = _safe_operational_status_page(
            ready=ready,
            spotify_state=self._spotify_state(),
            status=self.operational_status,
            build_revision=self.build_revision,
        )
        self._reply(
            HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
            payload,
        )

    def _serve_diagnostics(self, query: str) -> None:
        if self._require_admin() is None:
            return
        try:
            filters, events = self._diagnostic_events(query)
        except ValueError:
            self._invalid_diagnostic_filters()
            return
        except RuntimeError:
            self._diagnostic_history_unavailable()
            return
        self._reply(
            HTTPStatus.OK,
            render_diagnostics_page(
                events=events,
                filters=filters,
                display_timezone=self.diagnostic_timezone,
                timezone_label=self.diagnostic_timezone_label,
            ),
        )

    def _serve_diagnostics_export(self, query: str) -> None:
        if self._require_admin() is None:
            return
        store = self.diagnostic_store
        if store is None:
            self._diagnostic_history_unavailable()
            return
        try:
            filters, events = self._diagnostic_events(query)
        except ValueError:
            self._invalid_diagnostic_filters()
            return
        except RuntimeError:
            self._diagnostic_history_unavailable()
            return

        status = self.operational_status
        snapshot = None if status is None else status.snapshot()
        last_cycle = None if snapshot is None else snapshot.last_cycle
        payload = build_diagnostic_bundle(
            events=events,
            generated_at=datetime.now(UTC),
            last_cycle=last_cycle,
            retention_days=store.retention_days,
            max_events=store.max_events,
            build_revision=self.build_revision,
        )
        self._reply(
            HTTPStatus.OK,
            payload,
            content_type="application/zip",
            extra_headers={
                "Content-Disposition": 'attachment; filename="news-playlist-diagnostics.zip"'
            },
        )

    def _diagnostic_events(
        self,
        query: str,
    ) -> tuple[DiagnosticFilters, tuple[DiagnosticEvent, ...]]:
        store = self.diagnostic_store
        if store is None:
            raise RuntimeError("diagnostic store unavailable")
        filters = parse_diagnostic_filters(query)
        try:
            events = store.list_events(
                since=filters.since(now=datetime.now(UTC)),
                severity=filters.severity,
                source_id=filters.source_id,
                playlist_id=filters.playlist_id,
                limit=filters.limit,
            )
        except PersistenceError as exc:
            raise RuntimeError("diagnostic store unavailable") from exc
        return filters, events

    def _invalid_diagnostic_filters(self) -> None:
        self._reply(
            HTTPStatus.BAD_REQUEST,
            b"Invalid diagnostics filters",
            content_type="text/plain; charset=utf-8",
        )

    def _diagnostic_history_unavailable(self) -> None:
        self._reply(
            HTTPStatus.SERVICE_UNAVAILABLE,
            b"Diagnostic history is unavailable",
            content_type="text/plain; charset=utf-8",
        )


def serve(
    *,
    host: str = DEFAULT_HEALTH_HOST,
    port: int = DEFAULT_HEALTH_PORT,
    data_dir: Path = DEFAULT_DATA_DIR,
    stop_event: threading.Event | None = None,
    environ: Mapping[str, str] | None = None,
    interval: timedelta | None = None,
) -> int:
    """Run the production engine using the diagnostics-aware HTTP handler."""
    runtime_namespace = vars(engine_runtime)
    original_handler = runtime_namespace["OperationalHealthHandler"]
    handler = DiagnosticOperationalHealthHandler
    previous_timezone = handler.diagnostic_timezone
    previous_timezone_label = handler.diagnostic_timezone_label
    previous_build_revision = handler.build_revision
    display_timezone, timezone_label = _diagnostic_display_timezone(environ)
    handler.diagnostic_timezone = display_timezone
    handler.diagnostic_timezone_label = timezone_label
    handler.build_revision = resolve_build_revision(environ)
    runtime_namespace["OperationalHealthHandler"] = handler
    try:
        return engine_runtime.serve(
            host=host,
            port=port,
            data_dir=data_dir,
            stop_event=stop_event,
            environ=environ,
            interval=interval,
        )
    finally:
        handler.diagnostic_timezone = previous_timezone
        handler.diagnostic_timezone_label = previous_timezone_label
        handler.build_revision = previous_build_revision
        runtime_namespace["OperationalHealthHandler"] = original_handler


def _diagnostic_display_timezone(
    environ: Mapping[str, str] | None,
) -> tuple[tzinfo, str]:
    env = os.environ if environ is None else environ
    timezone_name = env.get("TZ", "").strip()
    if not timezone_name:
        return UTC, "UTC"
    try:
        return ZoneInfo(timezone_name), timezone_name
    except ZoneInfoNotFoundError, ValueError:
        return UTC, "UTC"


def _safe_operational_status_page(
    *,
    ready: bool,
    spotify_state: AuthorizationState | None,
    status: OperationalStatus | None,
    build_revision: str = "dev",
) -> bytes:
    snapshot = (
        OperationalStatus(configured=False).snapshot() if status is None else status.snapshot()
    )
    runtime_label = "Ready" if ready else "Degraded"
    storage_label = "Writable" if ready else "Unavailable"
    engine_label = "Not configured"
    if snapshot.configured:
        engine_label = "Cycle running" if snapshot.running else "Scheduled"
    cycle = snapshot.last_cycle
    cycle_result = "Not run yet" if cycle is None else ("Success" if cycle.ok else "Failed")
    cycle_started = "—" if cycle is None else _format_time(cycle.started_at)
    cycle_finished = "—" if cycle is None else _format_time(cycle.finished_at)
    next_run = "—" if snapshot.next_run_at is None else _format_time(snapshot.next_run_at)
    source_rows = _safe_source_rows(cycle)
    playlist_rows = _safe_playlist_rows(cycle)
    spotify_label = _spotify_label(spotify_state)

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>News Bulletin Playlists</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 72rem; margin: 3rem auto;
           padding: 0 1.25rem; line-height: 1.5; }}
    dl {{ display: grid; grid-template-columns: max-content 1fr; gap: .4rem 1rem; }}
    dt {{ font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; margin: 1rem 0 2rem; }}
    th, td {{ text-align: left; vertical-align: top; padding: .45rem .55rem;
              border-bottom: 1px solid #ddd; }}
    code {{ font-family: ui-monospace, monospace; }}
  </style>
</head>
<body>
  <h1>News Bulletin Playlists</h1>
  <p>This page is read-only. Detailed failure context is available only from the
     authenticated administration diagnostics.</p>
  <dl>
    <dt>Runtime</dt><dd>{runtime_label}</dd>
    <dt>Persistent storage</dt><dd>{storage_label}</dd>
    <dt>Spotify authorization</dt><dd>{spotify_label}</dd>
    <dt>Engine</dt><dd>{engine_label}</dd>
    <dt>Last cycle</dt><dd>{cycle_result}</dd>
    <dt>Cycle start</dt><dd>{cycle_started}</dd>
    <dt>Cycle end</dt><dd>{cycle_finished}</dd>
    <dt>Next run</dt><dd>{next_run}</dd>
    <dt>Version</dt><dd><code>{html.escape(__version__)}</code></dd>
    <dt>Build</dt><dd><code>{html.escape(build_revision)}</code></dd>
  </dl>
  <h2>Sources</h2>
  <table>
    <thead><tr>
      <th>Source</th><th>Result</th><th>Last success</th><th>Editions</th>
    </tr></thead>
    <tbody>{source_rows}</tbody>
  </table>
  <h2>Playlists</h2>
  <table>
    <thead><tr>
      <th>Playlist</th><th>Result</th><th>Last success</th><th>Items</th><th>Write</th>
    </tr></thead>
    <tbody>{playlist_rows}</tbody>
  </table>
</body>
</html>
"""
    return document.encode("utf-8")


def _safe_source_rows(cycle: EngineCycleResult | None) -> str:
    if cycle is None or not cycle.sources:
        return '<tr><td colspan="4">No source cycle data yet.</td></tr>'
    rows = []
    for source in cycle.sources:
        result = "Success" if source.ok else "Failed — see admin diagnostics"
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(source.source_id))}</code></td>"
            f"<td>{result}</td>"
            f"<td>{_format_optional_time(source.last_success_at)}</td>"
            f"<td>{source.edition_count} fetched / {source.matched_count} matched</td>"
            "</tr>"
        )
    return "".join(rows)


def _safe_playlist_rows(cycle: EngineCycleResult | None) -> str:
    if cycle is None or not cycle.playlists:
        return '<tr><td colspan="5">No playlist cycle data yet.</td></tr>'
    rows = []
    for playlist in cycle.playlists:
        result = "Success" if playlist.ok else "Failed — see admin diagnostics"
        applied = "unverified" if playlist.applied_count is None else str(playlist.applied_count)
        write = "—" if playlist.wrote is None else ("updated" if playlist.wrote else "unchanged")
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(playlist.playlist_id))}</code></td>"
            f"<td>{result}</td>"
            f"<td>{_format_optional_time(playlist.last_success_at)}</td>"
            f"<td>{playlist.desired_count} desired / {applied} verified</td>"
            f"<td>{write}</td>"
            "</tr>"
        )
    return "".join(rows)


def _spotify_label(state: AuthorizationState | None) -> str:
    if state is None:
        return "Not configured"
    return {
        AuthorizationState.DISCONNECTED: "Not connected",
        AuthorizationState.CONNECTED: "Connected",
        AuthorizationState.REAUTH_REQUIRED: "Reauthorization required",
        AuthorizationState.ERROR: "Authorization state error",
    }[state]


def _format_optional_time(value: datetime | None) -> str:
    return "—" if value is None else _format_time(value)


def _format_time(value: datetime) -> str:
    timestamp = value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return html.escape(timestamp)
