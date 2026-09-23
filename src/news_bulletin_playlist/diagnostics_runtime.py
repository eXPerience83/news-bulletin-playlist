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
from news_bulletin_playlist.diagnostics import MAX_DIAGNOSTIC_EXPORT_EVENTS, DiagnosticEvent
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
from news_bulletin_playlist.spotify.authorization_display import authorization_state_label
from news_bulletin_playlist.spotify_metrics import (
    SpotifyRequestMetrics,
    SpotifyRequestMetricsSnapshot,
)
from news_bulletin_playlist.web_ui import WEB_UI_STYLES, BadgeTone, status_badge


class DiagnosticOperationalHealthHandler(engine_runtime.OperationalHealthHandler):
    """Add private diagnostic routes and keep detailed failures off the public page."""

    diagnostic_timezone: tzinfo = UTC
    diagnostic_timezone_label = "UTC"
    build_revision = "dev"
    spotify_request_metrics: SpotifyRequestMetrics | None = None

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

        # Save is a local managed-state operation. Spotify metadata and cover are intentionally
        # synchronized only by the separate explicit action, so a disconnected/400 provider
        # cannot block source, duration, enable or desired-metadata changes.
        updated = service.update(
            playlist_id,
            display_name=name,
            description=description,
            cover_id=cover_id,
            source_ids=form.get("source_id", []),
            enabled=enabled,
            access_token=None,
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
            filters, events, _ = self._diagnostic_events(query)
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
                spotify_metrics=self._spotify_metrics(),
            ),
        )

    def _serve_diagnostics_export(self, query: str) -> None:
        if self._require_admin() is None:
            return
        store = self.diagnostic_store
        if store is None:
            self._diagnostic_history_unavailable()
            return
        generated_at = datetime.now(UTC)
        try:
            filters, events, result_truncated = self._diagnostic_export_events(
                query,
                observed_at=generated_at,
            )
            retention_status = store.retention_status()
        except ValueError:
            self._invalid_diagnostic_filters()
            return
        except RuntimeError, PersistenceError:
            self._diagnostic_history_unavailable()
            return

        status = self.operational_status
        snapshot = None if status is None else status.snapshot()
        last_cycle = None if snapshot is None else snapshot.last_cycle
        payload = build_diagnostic_bundle(
            events=events,
            generated_at=generated_at,
            last_cycle=last_cycle,
            retention_days=store.retention_days,
            max_events=store.max_events,
            build_revision=self.build_revision,
            filters=filters,
            result_truncated=result_truncated,
            spotify_metrics=self._spotify_metrics(),
            retention_status=retention_status,
            export_limit=MAX_DIAGNOSTIC_EXPORT_EVENTS,
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
    ) -> tuple[DiagnosticFilters, tuple[DiagnosticEvent, ...], bool | None]:
        store = self.diagnostic_store
        if store is None:
            raise RuntimeError("diagnostic store unavailable")
        filters = parse_diagnostic_filters(query)
        observed_at = datetime.now(UTC)
        since = filters.since(now=observed_at)
        try:
            events, result_truncated = store.list_events_with_truncation(
                since=since,
                severity=filters.severity,
                source_id=filters.source_id,
                playlist_id=filters.playlist_id,
                limit=filters.limit,
            )
        except PersistenceError as exc:
            raise RuntimeError("diagnostic store unavailable") from exc
        return filters, events, result_truncated

    def _diagnostic_export_events(
        self,
        query: str,
        *,
        observed_at: datetime,
    ) -> tuple[DiagnosticFilters, tuple[DiagnosticEvent, ...], bool]:
        store = self.diagnostic_store
        if store is None:
            raise RuntimeError("diagnostic store unavailable")
        filters = parse_diagnostic_filters(query)
        since = filters.since(now=observed_at)
        try:
            events, result_truncated = store.list_events_for_export_with_truncation(
                since=since,
                severity=filters.severity,
                source_id=filters.source_id,
                playlist_id=filters.playlist_id,
                limit=MAX_DIAGNOSTIC_EXPORT_EVENTS,
            )
        except PersistenceError as exc:
            raise RuntimeError("diagnostic store unavailable") from exc
        return filters, events, result_truncated

    def _spotify_metrics(self) -> SpotifyRequestMetricsSnapshot | None:
        metrics = self.spotify_request_metrics
        return None if metrics is None else metrics.last_24_hours()

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
    handler = DiagnosticOperationalHealthHandler
    previous_timezone = handler.diagnostic_timezone
    previous_timezone_label = handler.diagnostic_timezone_label
    previous_build_revision = handler.build_revision
    display_timezone, timezone_label = _diagnostic_display_timezone(environ)
    handler.diagnostic_timezone = display_timezone
    handler.diagnostic_timezone_label = timezone_label
    handler.build_revision = resolve_build_revision(environ)
    try:
        return engine_runtime.serve(
            host=host,
            port=port,
            data_dir=data_dir,
            stop_event=stop_event,
            environ=environ,
            interval=interval,
            handler_type=handler,
        )
    finally:
        handler.diagnostic_timezone = previous_timezone
        handler.diagnostic_timezone_label = previous_timezone_label
        handler.build_revision = previous_build_revision


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
    engine_tone: BadgeTone = "muted"
    if snapshot.configured:
        engine_label = "Cycle running" if snapshot.running else "Scheduled"
        engine_tone = "accent" if snapshot.running else "success"

    cycle = snapshot.last_cycle
    cycle_result = "Not run yet" if cycle is None else ("Success" if cycle.ok else "Failed")
    cycle_tone: BadgeTone = "muted" if cycle is None else ("success" if cycle.ok else "danger")
    cycle_started = "—" if cycle is None else _format_time(cycle.started_at)
    cycle_finished = "—" if cycle is None else _format_time(cycle.finished_at)
    next_run = "—" if snapshot.next_run_at is None else _format_time(snapshot.next_run_at)
    source_rows = _safe_source_rows(cycle)
    playlist_rows = _safe_playlist_rows(cycle)
    spotify_label = authorization_state_label(spotify_state)
    spotify_tone: BadgeTone = (
        "success" if spotify_state is AuthorizationState.CONNECTED else "muted"
    )
    runtime_tone: BadgeTone = "success" if ready else "danger"
    overall_label = "Operational"
    overall_tone: BadgeTone = "success"
    if not ready:
        overall_label = "Runtime degraded"
        overall_tone = "danger"
    elif cycle is not None and not cycle.ok:
        overall_label = "Cycle needs attention"
        overall_tone = "warning"
    elif snapshot.running:
        overall_label = "Cycle running"
        overall_tone = "accent"

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>News Bulletin Playlists</title>
  <style>{WEB_UI_STYLES}</style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <a class="brand" href="/" aria-label="News Bulletin Playlists status">
        <span class="brand-mark" aria-hidden="true">NB</span>
        <span>News Bulletin Playlists</span>
      </a>
      {status_badge(overall_label, tone=overall_tone)}
    </header>

    <main id="main-content">
      <section class="hero" aria-labelledby="page-title">
        <div>
          <p class="eyebrow">Live operational status</p>
          <h1 id="page-title">Your news playlists, always current.</h1>
          <p class="hero-copy">
            A read-only view of the engine, Spotify connection, sources and published playlists.
            Detailed failure context stays inside authenticated administration.
          </p>
        </div>
        <div class="hero-actions">
          <a class="button button-primary" href="/admin/">Administration</a>
          <span class="section-note">Configuration and diagnostics are protected.</span>
        </div>
      </section>

      <section aria-label="Runtime summary">
        <div class="summary-grid">
          <article class="card summary-card">
            <p class="card-label">Runtime</p>
            <p class="card-value">{status_badge(runtime_label, tone=runtime_tone)}</p>
            <p class="card-meta">Persistent storage: {html.escape(storage_label)}</p>
          </article>
          <article class="card summary-card">
            <p class="card-label">Spotify</p>
            <p class="card-value">{status_badge(spotify_label, tone=spotify_tone)}</p>
            <p class="card-meta">Authorization state</p>
          </article>
          <article class="card summary-card">
            <p class="card-label">Engine</p>
            <p class="card-value">{status_badge(engine_label, tone=engine_tone)}</p>
            <p class="card-meta">Next run: {next_run}</p>
          </article>
          <article class="card summary-card">
            <p class="card-label">Last cycle</p>
            <p class="card-value">{status_badge(cycle_result, tone=cycle_tone)}</p>
            <p class="card-meta">Started {cycle_started} · ended {cycle_finished}</p>
          </article>
        </div>
      </section>

      <section class="section" aria-labelledby="sources-heading">
        <div class="section-head">
          <div>
            <p class="eyebrow">Collection</p>
            <h2 id="sources-heading">Sources</h2>
          </div>
          <p class="section-note">Public-safe source health and catalogue freshness.</p>
        </div>
        <div class="card table-card">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Source</th>
                  <th scope="col">Result</th>
                  <th scope="col">Detector success</th>
                  <th scope="col">Catalogue refresh</th>
                  <th scope="col">Spotify state</th>
                </tr>
              </thead>
              <tbody>{source_rows}</tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="section" aria-labelledby="playlists-heading">
        <div class="section-head">
          <div>
            <p class="eyebrow">Publishing</p>
            <h2 id="playlists-heading">Playlists</h2>
          </div>
          <p class="section-note">Desired, verified and write state from the latest cycle.</p>
        </div>
        <div class="card table-card">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Playlist</th>
                  <th scope="col">Result</th>
                  <th scope="col">Last success</th>
                  <th scope="col">Items</th>
                  <th scope="col">Write</th>
                </tr>
              </thead>
              <tbody>{playlist_rows}</tbody>
            </table>
          </div>
        </div>
      </section>
    </main>

    <footer class="footer">
      <p>Read-only status · sensitive diagnostics are not exposed here.</p>
      <p>Version <code>{html.escape(__version__)}</code> · Build
        <code>{html.escape(build_revision)}</code></p>
    </footer>
  </div>
</body>
</html>
"""
    return document.encode("utf-8")


def _safe_source_rows(cycle: EngineCycleResult | None) -> str:
    if cycle is None or not cycle.sources:
        return '<tr><td colspan="5">No source cycle data yet.</td></tr>'
    rows = []
    for source in cycle.sources:
        result = (
            status_badge("Success", tone="success")
            if source.ok
            else status_badge("Failed — see admin diagnostics", tone="danger")
        )
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(source.source_id))}</code></td>"
            f"<td>{result}</td>"
            f"<td>{_format_optional_time(source.last_detector_success_at)}</td>"
            f"<td>{_format_optional_time(source.last_catalogue_refresh_at)}</td>"
            f"<td>{source.episode_count} cached / {source.api_request_count} API requests</td>"
            "</tr>"
        )
    return "".join(rows)


def _safe_playlist_rows(cycle: EngineCycleResult | None) -> str:
    if cycle is None or not cycle.playlists:
        return '<tr><td colspan="5">No playlist cycle data yet.</td></tr>'
    rows = []
    for playlist in cycle.playlists:
        result = (
            status_badge("Success", tone="success")
            if playlist.ok
            else status_badge("Failed — see admin diagnostics", tone="danger")
        )
        applied = "unverified" if playlist.applied_count is None else str(playlist.applied_count)
        write = "—" if playlist.wrote is None else ("updated" if playlist.wrote else "unchanged")
        write_tone: BadgeTone = (
            "muted" if playlist.wrote is None else ("accent" if playlist.wrote else "success")
        )
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(playlist.playlist_id))}</code></td>"
            f"<td>{result}</td>"
            f"<td>{_format_optional_time(playlist.last_success_at)}</td>"
            f"<td>{playlist.desired_count} desired / {applied} verified</td>"
            f"<td>{status_badge(write, tone=write_tone)}</td>"
            "</tr>"
        )
    return "".join(rows)


def _format_optional_time(value: datetime | None) -> str:
    return "—" if value is None else _format_time(value)


def _format_time(value: datetime) -> str:
    timestamp = value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return html.escape(timestamp)
