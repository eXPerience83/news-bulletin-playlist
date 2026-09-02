"""Authenticated diagnostic rendering and export helpers using sanitized models only."""

from __future__ import annotations

import html
import io
import json
import re
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from news_bulletin_playlist import __version__
from news_bulletin_playlist.diagnostics import (
    MAX_DIAGNOSTIC_QUERY_LIMIT,
    DiagnosticEvent,
    DiagnosticSeverity,
)
from news_bulletin_playlist.engine import EngineCycleResult

MAX_DIAGNOSTIC_FILTER_HOURS = 30 * 24
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ALLOWED_QUERY_KEYS = frozenset({"severity", "source", "playlist", "hours", "limit"})


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

    def query_string(self) -> str:
        pairs: list[tuple[str, str]] = []
        if self.severity is not None:
            pairs.append(("severity", self.severity.value))
        if self.source_id is not None:
            pairs.append(("source", self.source_id))
        if self.playlist_id is not None:
            pairs.append(("playlist", self.playlist_id))
        pairs.append(("hours", "all" if self.hours is None else str(self.hours)))
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
) -> bytes:
    """Render authenticated diagnostics without raw provider/error data."""
    rows = "".join(
        _event_row(event, display_timezone=display_timezone) for event in events
    )
    if not rows:
        rows = '<tr><td colspan="8">No diagnostic events match these filters.</td></tr>'
    export_href = "/admin/diagnostics/export.zip?" + filters.query_string()
    severity_value = "" if filters.severity is None else filters.severity.value
    source_value = "" if filters.source_id is None else filters.source_id
    playlist_value = "" if filters.playlist_id is None else filters.playlist_id
    hours_value = "all" if filters.hours is None else str(filters.hours)
    timezone_heading = html.escape(timezone_label)

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Diagnostics · News Bulletin Playlists</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: system-ui, sans-serif; max-width: 92rem; margin: 2rem auto;
           padding: 0 1rem 4rem; line-height: 1.4; }}
    form {{ display: flex; gap: .75rem; align-items: end; flex-wrap: wrap; }}
    label {{ display: grid; gap: .25rem; font-weight: 650; }}
    input, select, button {{ font: inherit; padding: .4rem .5rem; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 1.25rem; }}
    th, td {{ text-align: left; vertical-align: top; border-bottom: 1px solid #8885;
              padding: .45rem .4rem; }}
    code {{ font-family: ui-monospace, monospace; overflow-wrap: anywhere; }}
    .muted {{ opacity: .75; }}
  </style>
</head>
<body>
  <h1>Diagnostics</h1>
  <p class="muted">Structured, sanitized operational events only. Raw SQLite, OAuth material,
     provider response bodies and environment variables are never exposed here.</p>
  <form method="get" action="/admin/diagnostics">
    <label>Severity
      <select name="severity">{_severity_options(severity_value)}</select>
    </label>
    <label>Source
      <input name="source" value="{html.escape(source_value, quote=True)}" placeholder="rne">
    </label>
    <label>Playlist
      <input name="playlist" value="{html.escape(playlist_value, quote=True)}"
             placeholder="spain_spanish_news">
    </label>
    <label>Window
      <select name="hours">{_hours_options(hours_value)}</select>
    </label>
    <label>Rows
      <input type="number" name="limit" min="1" max="{MAX_DIAGNOSTIC_QUERY_LIMIT}"
             value="{filters.limit}">
    </label>
    <button type="submit">Apply filters</button>
  </form>
  <p><a href="{html.escape(export_href, quote=True)}">Download sanitized diagnostics ZIP</a></p>
  <table>
    <thead><tr>
      <th>Time ({timezone_heading})</th><th>Level</th><th>Component</th><th>Event</th>
      <th>Cycle</th><th>Source</th><th>Playlist</th><th>Details</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <p><a href="/admin/">Return to administration</a></p>
</body>
</html>
"""
    return document.encode("utf-8")


def build_diagnostic_bundle(
    *,
    events: tuple[DiagnosticEvent, ...],
    generated_at: datetime,
    last_cycle: EngineCycleResult | None,
    retention_days: int,
    max_events: int,
) -> bytes:
    """Build an in-memory ZIP from sanitized models; never copy the SQLite file."""
    observed = _as_utc(generated_at)
    event_documents = [_event_document(event) for event in events]
    jsonl = "".join(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for document in event_documents
    )
    readable = "".join(_event_text(document) for document in event_documents)
    runtime = {
        "generated_at": _timestamp(observed),
        "version": __version__,
        "diagnostic_retention_days": retention_days,
        "diagnostic_store_max_events": max_events,
        "exported_event_count": len(events),
        "export_event_limit": MAX_DIAGNOSTIC_QUERY_LIMIT,
    }
    manifest = {
        "format": "news-bulletin-playlist-diagnostics-v1",
        "generated_at": _timestamp(observed),
        "files": ["diagnostics.jsonl", "diagnostics.txt", "status.json", "runtime.json"],
        "privacy": "sanitized structured diagnostics; no raw database or credentials",
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _writestr(archive, "diagnostics.jsonl", jsonl)
        _writestr(archive, "diagnostics.txt", readable)
        _writestr(archive, "status.json", _json_text(_safe_status_document(last_cycle)))
        _writestr(archive, "runtime.json", _json_text(runtime))
        _writestr(archive, "manifest.json", _json_text(manifest))
    return buffer.getvalue()


def _event_document(event: DiagnosticEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "occurred_at": _timestamp(event.occurred_at),
        "severity": event.severity.value,
        "component": event.component,
        "event_name": event.event_name,
        "cycle_id": event.cycle_id,
        "source_id": event.source_id,
        "playlist_id": event.playlist_id,
        "details": event.details,
    }


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
                    "collection_ok": source.collection_ok,
                    "matching_ok": source.matching_ok,
                    "edition_count": source.edition_count,
                    "matched_count": source.matched_count,
                    "last_success_at": _optional_timestamp(source.last_success_at),
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
    details = " ".join(
        f"{key}={_plain_value(value)}" for key, value in sorted(event.details.items())
    ) or "—"
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
    detail_text = " ".join(
        f"{key}={_plain_value(value)}" for key, value in sorted(details.items())
    )
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
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, content.encode("utf-8"))


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
