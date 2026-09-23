"""Authenticated diagnostic rendering and export helpers with bounded public failure evidence."""

from __future__ import annotations

import base64
import html
import io
import json
import re
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from news_bulletin_playlist import __version__
from news_bulletin_playlist.admin_web import render_admin_shell
from news_bulletin_playlist.diagnostics import (
    MAX_DIAGNOSTIC_EXPORT_EVENTS,
    MAX_DIAGNOSTIC_QUERY_LIMIT,
    DiagnosticEvent,
    DiagnosticRetentionStatus,
    DiagnosticSeverity,
)
from news_bulletin_playlist.engine import EngineCycleResult
from news_bulletin_playlist.spotify_metrics import (
    SpotifyRequestMetric,
    SpotifyRequestMetricsSnapshot,
)

MAX_DIAGNOSTIC_FILTER_HOURS = 30 * 24
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ALLOWED_QUERY_KEYS = frozenset({"severity", "source", "playlist", "hours", "limit"})
_PUBLIC_RESPONSE_BODY_KEY = "public_response_body_b64"


@dataclass(frozen=True, slots=True)
class DiagnosticFilters:
    severity: DiagnosticSeverity | None = None
    source_id: str | None = None
    playlist_id: str | None = None
    hours: int | None = 24
    limit: int = 200

    def since(self, *, now: datetime) -> datetime | None:
        observed = _as_utc(now)
        return None if self.hours is None else observed - timedelta(hours=self.hours)

    def query_string(self, *, include_limit: bool = True) -> str:
        pairs: list[tuple[str, str]] = []
        if self.severity is not None:
            pairs.append(("severity", self.severity.value))
        if self.source_id is not None:
            pairs.append(("source", self.source_id))
        if self.playlist_id is not None:
            pairs.append(("playlist", self.playlist_id))
        pairs.append(("hours", "all" if self.hours is None else str(self.hours)))
        if include_limit:
            pairs.append(("limit", str(self.limit)))
        return urllib.parse.urlencode(pairs)


def parse_diagnostic_filters(query: str) -> DiagnosticFilters:
    """Parse one bounded, single-valued authenticated diagnostics query."""
    values = urllib.parse.parse_qs(query, keep_blank_values=True, strict_parsing=False)
    unknown = sorted(set(values) - _ALLOWED_QUERY_KEYS)
    if unknown:
        raise ValueError(f"unknown diagnostics filter: {unknown[0]}")
    for key, items in values.items():
        if len(items) != 1:
            raise ValueError(f"diagnostics filter {key} must be supplied at most once")

    severity_raw = _single(values, "severity")
    severity: DiagnosticSeverity | None = None
    if severity_raw:
        try:
            severity = DiagnosticSeverity(severity_raw.upper())
        except ValueError as exc:
            raise ValueError("diagnostics severity must be INFO, WARNING or ERROR") from exc

    source_id = _optional_identifier(_single(values, "source"), "source")
    playlist_id = _optional_identifier(_single(values, "playlist"), "playlist")

    hours_raw = _single(values, "hours")
    if hours_raw == "all":
        hours = None
    elif hours_raw:
        hours = _bounded_int(
            hours_raw,
            "diagnostics hours",
            minimum=1,
            maximum=MAX_DIAGNOSTIC_FILTER_HOURS,
        )
    else:
        hours = 24

    limit_raw = _single(values, "limit")
    limit = (
        200
        if not limit_raw
        else _bounded_int(
            limit_raw,
            "diagnostics limit",
            minimum=1,
            maximum=MAX_DIAGNOSTIC_QUERY_LIMIT,
        )
    )
    return DiagnosticFilters(
        severity=severity,
        source_id=source_id,
        playlist_id=playlist_id,
        hours=hours,
        limit=limit,
    )


def render_diagnostics_page(
    *,
    events: tuple[DiagnosticEvent, ...],
    filters: DiagnosticFilters,
    display_timezone: tzinfo = UTC,
    timezone_label: str = "UTC",
    spotify_metrics: SpotifyRequestMetricsSnapshot | None = None,
) -> bytes:
    """Render authenticated diagnostics directly inside the shared administration shell."""

    rows = "".join(_event_row(event, display_timezone=display_timezone) for event in events)
    if not rows:
        rows = '<tr><td colspan="8">No diagnostic events match these filters.</td></tr>'
    export_href = "/admin/diagnostics/export.zip?" + filters.query_string(include_limit=False)
    severity_value = "" if filters.severity is None else filters.severity.value
    source_value = "" if filters.source_id is None else filters.source_id
    playlist_value = "" if filters.playlist_id is None else filters.playlist_id
    hours_value = "all" if filters.hours is None else str(filters.hours)
    metrics = _spotify_metrics_section(spotify_metrics)
    body = f"""
<section class="section card admin-panel" aria-labelledby="diagnostic-filters-heading">
  <h2 id="diagnostic-filters-heading">Filters</h2>
  <form class="filter-form" method="get" action="/admin/diagnostics">
    <label>Severity
      <select name="severity">{_severity_options(severity_value)}</select>
    </label>
    <label>Source
      <input type="text" name="source" value="{html.escape(source_value, quote=True)}"
             placeholder="rne">
    </label>
    <label>Playlist
      <input type="text" name="playlist" value="{html.escape(playlist_value, quote=True)}"
             placeholder="spain_spanish_news">
    </label>
    <label>Window
      <select name="hours">{_hours_options(hours_value)}</select>
    </label>
    <label>Rows
      <input type="number" name="limit" min="1" max="{MAX_DIAGNOSTIC_QUERY_LIMIT}"
             value="{filters.limit}">
    </label>
    <button class="button" type="submit">Apply filters</button>
  </form>
  <p class="muted">UI limit={MAX_DIAGNOSTIC_QUERY_LIMIT}; ZIP export uses a separate bounded
     capacity. Raw SQLite, OAuth material, authenticated provider response bodies and environment
     variables are never exposed here. A bounded response from an unauthenticated public Spotify
     schema failure may be retained only as a separate file in the authenticated ZIP.</p>
  <p><a class="button" href="{html.escape(export_href, quote=True)}">Download sanitized
     diagnostics ZIP</a></p>
</section>
<section class="section card admin-panel" aria-labelledby="spotify-request-metrics-heading">
  {metrics}
</section>
<section class="section admin-section" aria-labelledby="diagnostic-events-heading">
  <div class="section-head">
    <div>
      <p class="eyebrow">History</p>
      <h2 id="diagnostic-events-heading">Structured events</h2>
    </div>
    <p class="section-note">Times shown in {html.escape(timezone_label)}</p>
  </div>
  <div class="card table-card"><div class="table-scroll">
    <table>
      <thead><tr>
        <th scope="col">Time ({html.escape(timezone_label)})</th>
        <th scope="col">Level</th><th scope="col">Component</th><th scope="col">Event</th>
        <th scope="col">Cycle</th><th scope="col">Source</th><th scope="col">Playlist</th>
        <th scope="col">Details</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div></div>
</section>
"""
    return render_admin_shell(
        title="Diagnostics",
        active_tab="diagnostics",
        intro=(
            "Inspect sanitized operational events, Spotify request aggregates and exportable "
            "diagnostic evidence."
        ),
        body_html=body,
    )


def build_diagnostic_bundle(
    *,
    events: tuple[DiagnosticEvent, ...],
    generated_at: datetime,
    last_cycle: EngineCycleResult | None,
    retention_days: int,
    max_events: int,
    build_revision: str = "dev",
    filters: DiagnosticFilters | None = None,
    result_truncated: bool | None = False,
    spotify_metrics: SpotifyRequestMetricsSnapshot | None = None,
    retention_status: DiagnosticRetentionStatus | None = None,
    export_limit: int | None = None,
) -> bytes:
    """Build an in-memory ZIP from sanitized models; never copy the SQLite file."""
    observed = _as_utc(generated_at)
    attachments = tuple(
        attachment
        for event in events
        if (attachment := _public_response_attachment(event)) is not None
    )
    event_documents = [_event_document(event) for event in events]
    jsonl = "".join(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for document in event_documents
    )
    readable = "".join(_event_text(document) for document in event_documents)
    effective_filters = filters or DiagnosticFilters()
    requested_limit = effective_filters.limit if export_limit is None else export_limit
    allowed_limit = (
        MAX_DIAGNOSTIC_QUERY_LIMIT if export_limit is None else MAX_DIAGNOSTIC_EXPORT_EVENTS
    )
    oldest = None if retention_status is None else retention_status.oldest_occurred_at
    newest = None if retention_status is None else retention_status.newest_occurred_at
    runtime = {
        "generated_at": _timestamp(observed),
        "version": __version__,
        "build_revision": build_revision,
        "diagnostic_retention_days": retention_days,
        "diagnostic_store_max_events": max_events,
        "diagnostic_store_event_count": (
            None if retention_status is None else retention_status.event_count
        ),
        "diagnostic_store_oldest_occurred_at": (None if oldest is None else _timestamp(oldest)),
        "diagnostic_store_newest_occurred_at": (None if newest is None else _timestamp(newest)),
        "diagnostic_store_capacity_limit_reached": (
            None if retention_status is None else retention_status.capacity_limit_reached
        ),
        "ui_max_query_limit": MAX_DIAGNOSTIC_QUERY_LIMIT,
        "exported_event_count": len(events),
        "export_requested_limit": requested_limit,
        "export_max_allowed_limit": allowed_limit,
        "export_result_truncated": result_truncated,
        "export_first_event_id": events[0].event_id if events else None,
        "export_last_event_id": events[-1].event_id if events else None,
        "export_first_occurred_at": _timestamp(events[0].occurred_at) if events else None,
        "export_last_occurred_at": _timestamp(events[-1].occurred_at) if events else None,
        "export_filters": {
            "severity": (
                None if effective_filters.severity is None else effective_filters.severity.value
            ),
            "source_id": effective_filters.source_id,
            "playlist_id": effective_filters.playlist_id,
            "hours": effective_filters.hours,
        },
        "spotify_request_metrics_last_24_hours": _spotify_metrics_document(spotify_metrics),
    }
    manifest = {
        "format": "news-bulletin-playlist-diagnostics-v1",
        "generated_at": _timestamp(observed),
        "files": [
            "diagnostics.jsonl",
            "diagnostics.txt",
            "status.json",
            "runtime.json",
            *[name for name, _body in attachments],
        ],
        "privacy": (
            "structured diagnostics plus bounded unauthenticated public Spotify "
            "schema-failure response captures; no raw database or credentials"
        ),
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _writestr(archive, "diagnostics.jsonl", jsonl)
        _writestr(archive, "diagnostics.txt", readable)
        _writestr(archive, "status.json", _json_text(_safe_status_document(last_cycle)))
        _writestr(archive, "runtime.json", _json_text(runtime))
        for name, body in attachments:
            _writebytes(archive, name, body)
        _writestr(archive, "manifest.json", _json_text(manifest))
    return buffer.getvalue()


def _event_document(event: DiagnosticEvent) -> dict[str, object]:
    details = dict(event.details)
    details.pop(_PUBLIC_RESPONSE_BODY_KEY, None)
    attachment = _public_response_attachment(event)
    if attachment is not None:
        details["public_response_attachment"] = attachment[0]
    return {
        "event_id": event.event_id,
        "occurred_at": _timestamp(event.occurred_at),
        "severity": event.severity.value,
        "component": event.component,
        "event_name": event.event_name,
        "cycle_id": event.cycle_id,
        "source_id": event.source_id,
        "playlist_id": event.playlist_id,
        "details": details,
    }


def _public_response_attachment(event: DiagnosticEvent) -> tuple[str, bytes] | None:
    encoded = event.details.get(_PUBLIC_RESPONSE_BODY_KEY)
    if not isinstance(encoded, str):
        return None
    body = base64.b64decode(encoded, validate=True)
    surface_value = event.details.get("public_response_surface")
    surface = surface_value if isinstance(surface_value, str) else "public"
    content_type_value = event.details.get("public_response_content_type")
    content_type = content_type_value if isinstance(content_type_value, str) else ""
    extension = (
        "html"
        if "html" in content_type.casefold()
        else "json"
        if "json" in content_type.casefold()
        else "bin"
    )
    source = event.source_id or "unknown"
    return (
        f"public-responses/event-{event.event_id}-{source}-{surface}.{extension}",
        body,
    )


def _spotify_metrics_document(
    snapshot: SpotifyRequestMetricsSnapshot | None,
) -> dict[str, object] | None:
    if snapshot is None:
        return None
    counts = {
        "public_requests": snapshot.public_requests,
        "web_api_requests": snapshot.web_api_requests,
        "oauth_requests": snapshot.oauth_requests,
        "oauth_refreshes": snapshot.oauth_refreshes,
        "http_429s": snapshot.http_rate_limits,
        "quota_exceeded": snapshot.quota_exceeded,
    }
    return {
        "coverage_complete": snapshot.coverage_complete,
        **(counts if snapshot.coverage_complete else {key: None for key in counts}),
        "recorded_counts": None if snapshot.coverage_complete else counts,
        "breakdown": [
            {
                "surface": metric.surface,
                "operation": metric.operation,
                "outcome": metric.outcome.value,
                "http_status": metric.http_status,
                "quota_reason": None if metric.quota_reason is None else metric.quota_reason.value,
                "request_count": metric.request_count,
            }
            for metric in snapshot.metrics
        ],
    }


def _spotify_metrics_section(snapshot: SpotifyRequestMetricsSnapshot | None) -> str:
    heading = (
        '<h2 id="spotify-request-metrics-heading">'
        "Spotify request metrics (last 24 hours)</h2>"
    )
    if snapshot is None:
        return heading + '<p class="muted">Spotify request metrics are unavailable.</p>'

    def quota_reason(metric: SpotifyRequestMetric) -> str:
        return "—" if metric.quota_reason is None else metric.quota_reason.value

    rows = (
        "".join(
            "<tr>"
            f"<td><code>{html.escape(metric.surface)}</code></td>"
            f"<td><code>{html.escape(metric.operation)}</code></td>"
            f"<td><code>{html.escape(metric.outcome.value)}</code></td>"
            f"<td>{'—' if metric.http_status is None else metric.http_status}</td>"
            f"<td><code>{html.escape(quota_reason(metric))}</code></td>"
            f"<td>{metric.request_count}</td>"
            "</tr>"
            for metric in snapshot.metrics
        )
        or '<tr><td colspan="6">No recorded Spotify HTTP attempts in this window.</td></tr>'
    )
    summary = (
        "Coverage: complete. "
        f"Public Spotify requests (no OAuth): {snapshot.public_requests}. "
        f"Spotify Web API requests (authenticated): {snapshot.web_api_requests}. "
        f"OAuth token requests: {snapshot.oauth_requests}. "
        f"HTTP 429s: {snapshot.http_rate_limits}. "
        f"QUOTA_EXCEEDED: {snapshot.quota_exceeded}."
        if snapshot.coverage_complete
        else (
            "Coverage: incomplete. Totals are unavailable; "
            "the rows below show only recorded attempts."
        )
    )
    return f"""
  {heading}
  <p class="muted">{summary} These are local aggregates, not account quota.</p>
  <div class="table-scroll">
    <table><thead><tr>
      <th scope="col">Surface</th><th scope="col">Operation</th>
      <th scope="col">Outcome</th><th scope="col">HTTP</th>
      <th scope="col">Quota reason</th><th scope="col">Requests</th>
    </tr></thead><tbody>{rows}</tbody></table>
  </div>
"""


def _safe_status_document(cycle: EngineCycleResult | None) -> dict[str, object]:
    if cycle is None:
        return {"last_cycle": None}
    return {
        "last_cycle": {
            "started_at": _timestamp(cycle.started_at),
            "finished_at": _timestamp(cycle.finished_at),
            "ok": cycle.ok,
            "sources": [
                {
                    "source_id": str(source.source_id),
                    "ok": source.ok,
                    "episode_count": source.episode_count,
                    "refreshed": source.refreshed,
                    "refresh_reason": source.refresh_reason,
                    "api_request_count": source.api_request_count,
                    "detector_ok": source.detector_ok,
                    "detector_changed": source.detector_changed,
                    "detector_false_negative": source.detector_false_negative,
                    "parser_fallback_count": source.parser_fallback_count,
                    "filter_rejected_count": source.filter_rejected_count,
                    "removed_count": source.removed_count,
                    "last_detector_success_at": _optional_timestamp(
                        source.last_detector_success_at
                    ),
                    "last_catalogue_refresh_at": _optional_timestamp(
                        source.last_catalogue_refresh_at
                    ),
                }
                for source in cycle.sources
            ],
            "playlists": [
                {
                    "playlist_id": str(playlist.playlist_id),
                    "ok": playlist.ok,
                    "desired_count": playlist.desired_count,
                    "applied_count": playlist.applied_count,
                    "wrote": playlist.wrote,
                    "last_success_at": _optional_timestamp(playlist.last_success_at),
                }
                for playlist in cycle.playlists
            ],
        }
    }


def _event_row(event: DiagnosticEvent, *, display_timezone: tzinfo) -> str:
    visible_details = {
        key: value for key, value in event.details.items() if key != _PUBLIC_RESPONSE_BODY_KEY
    }
    detail_parts = [
        f"{key}={_plain_value(value)}" for key, value in sorted(visible_details.items())
    ]
    if _PUBLIC_RESPONSE_BODY_KEY in event.details:
        detail_parts.append("public_response_body=<captured in ZIP>")
    details = " ".join(detail_parts) or "—"
    occurred_at = html.escape(_display_timestamp(event.occurred_at, display_timezone))
    return (
        "<tr>"
        f"<td><code>{occurred_at}</code></td>"
        f"<td>{html.escape(event.severity.value)}</td>"
        f"<td><code>{html.escape(event.component)}</code></td>"
        f"<td><code>{html.escape(event.event_name)}</code></td>"
        f"<td><code>{_optional_html(event.cycle_id)}</code></td>"
        f"<td><code>{_optional_html(event.source_id)}</code></td>"
        f"<td><code>{_optional_html(event.playlist_id)}</code></td>"
        f"<td><code>{html.escape(details)}</code></td>"
        "</tr>"
    )


def _event_text(document: dict[str, object]) -> str:
    details = document["details"]
    assert isinstance(details, dict)
    detail_text = " ".join(f"{key}={_plain_value(value)}" for key, value in sorted(details.items()))
    fields = [
        str(document["occurred_at"]),
        str(document["severity"]),
        f"component={document['component']}",
        f"event={document['event_name']}",
    ]
    for key in ("cycle_id", "source_id", "playlist_id"):
        value = document[key]
        if value is not None:
            fields.append(f"{key}={value}")
    if detail_text:
        fields.append(detail_text)
    return " ".join(fields) + "\n"


def _severity_options(selected: str) -> str:
    options = (
        ("", "All"),
        ("INFO", "INFO"),
        ("WARNING", "WARNING"),
        ("ERROR", "ERROR"),
    )
    return "".join(_option(value, label, selected) for value, label in options)


def _hours_options(selected: str) -> str:
    options = (
        ("1", "1 hour"),
        ("6", "6 hours"),
        ("24", "24 hours"),
        ("168", "7 days"),
        ("720", "30 days"),
        ("all", "All retained"),
    )
    return "".join(_option(value, label, selected) for value, label in options)


def _option(value: str, label: str, selected: str) -> str:
    marker = " selected" if value == selected else ""
    escaped = html.escape(value, quote=True)
    return f'<option value="{escaped}"{marker}>{html.escape(label)}</option>'


def _single(values: dict[str, list[str]], key: str) -> str:
    items = values.get(key)
    return "" if not items else items[0].strip()


def _optional_identifier(value: str, label: str) -> str | None:
    if not value:
        return None
    if len(value) > 128 or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"diagnostics {label} contains an invalid identifier")
    return value


def _bounded_int(value: str, label: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _optional_html(value: str | None) -> str:
    return "—" if value is None else html.escape(value)


def _plain_value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _writestr(archive: zipfile.ZipFile, name: str, content: str) -> None:
    _writebytes(archive, name, content.encode("utf-8"))


def _writebytes(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, content)


def _display_timestamp(value: datetime, display_timezone: tzinfo) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("diagnostics timestamp must be timezone-aware")
    return value.astimezone(display_timezone).isoformat(timespec="seconds")


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _optional_timestamp(value: datetime | None) -> str | None:
    return None if value is None else _timestamp(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("diagnostics timestamp must be timezone-aware")
    return value.astimezone(UTC)
