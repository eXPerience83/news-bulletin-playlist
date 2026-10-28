"""Bounded JSON upload and escaped server-rendered source import forms."""

from __future__ import annotations

import html
import re
from dataclasses import fields
from email import policy
from email.parser import BytesParser

from news_bulletin_playlist.admin_web import render_admin_shell, render_lan_warning
from news_bulletin_playlist.models import SourceDefinition
from news_bulletin_playlist.portable_sources import (
    MAX_SOURCE_DOCUMENT_BYTES,
    UnsupportedPortableSource,
)
from news_bulletin_playlist.source_import import SourceImportPreview, SourceImportResult

MAX_SOURCE_IMPORT_MULTIPART_BYTES = MAX_SOURCE_DOCUMENT_BYTES + 8192


def parse_source_import_upload(content_type: str, body: bytes) -> tuple[str, bytes]:
    """Accept exactly one CSRF field and one raw JSON file, never a filesystem path.

    Uses the same bounded MIME/framing rules as owner-cover upload, with a separate
    smaller budget and field/MIME policy. Filename is untrusted and never used.
    """
    if len(content_type) > 180 or not 0 < len(body) <= MAX_SOURCE_IMPORT_MULTIPART_BYTES:
        raise ValueError("Invalid source upload size or content type")
    match = re.fullmatch(
        r'multipart/form-data;\s*boundary=(?:"([^"\\]{1,100})"|([^;\s]{1,100}))',
        content_type,
        re.IGNORECASE,
    )
    boundary = "" if match is None else (match[1] or match[2])
    if re.fullmatch(r"[A-Za-z0-9_.'()+,/:=?-]{1,100}", boundary) is None:
        raise ValueError("Invalid source upload boundary")
    marker = boundary.encode("ascii")
    if not body.startswith(b"--" + marker + b"\r\n") or not body.endswith(
        b"--" + marker + b"--\r\n"
    ):
        raise ValueError("Incomplete source upload")
    message = BytesParser(policy=policy.default).parsebytes(
        b"MIME-Version: 1.0\r\nContent-Type: multipart/form-data; boundary="
        + marker
        + b"\r\n\r\n"
        + body
    )
    if not message.is_multipart() or message.defects:
        raise ValueError("Invalid source upload")
    values: dict[str, bytes] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if (
            part.defects
            or part.is_multipart()
            or part.get_content_disposition() != "form-data"
            or not isinstance(name, str)
            or name not in {"csrf_token", "document"}
            or name in values
            or len(part.get_all("Content-Disposition", [])) != 1
            or len(part.get_all("Content-Type", [])) > 1
            or len(part.get_all("Content-Transfer-Encoding", [])) > 1
            or part.get("Content-Transfer-Encoding", "binary").lower() not in {"binary", "8bit"}
        ):
            raise ValueError("Invalid or duplicate source upload field")
        data = part.get_payload(decode=True)
        limit = MAX_SOURCE_DOCUMENT_BYTES if name == "document" else 128
        if not isinstance(data, bytes) or not 0 < len(data) <= limit:
            raise ValueError("Invalid source upload field size")
        if name == "document":
            if part.get_content_type() not in {
                "application/json",
                "application/octet-stream",
                "text/plain",
            }:
                raise ValueError("Expected a JSON source document")
        elif part.get_filename() is not None:
            raise ValueError("Invalid source upload CSRF field")
        values[name] = data
    if values.keys() != {"csrf_token", "document"}:
        raise ValueError("Expected a CSRF field and one JSON source document")
    token = values["csrf_token"].decode("ascii")
    if re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token) is None:
        raise ValueError("Invalid source upload CSRF field")
    return token, values["document"]


def _page(body: str, *, lan_mode: bool) -> bytes:
    return render_admin_shell(
        title="Import sources",
        active_tab="sources",
        intro="Import portable source definitions without contacting Spotify.",
        body_html=(render_lan_warning() if lan_mode else "")
        + '<section class="section admin-section">'
        + body
        + "</section>",
    )


def render_source_import_upload(*, csrf_token: str, lan_mode: bool) -> bytes:
    return _page(
        f'''<h2>Import sources</h2>
<p>Select an exported sources JSON v1 file (up to 256 KiB and 100 sources).</p>
<p>Preview does not install anything. Existing sources are never overwritten.</p>
<form method="post" action="/admin/sources/import/preview" enctype="multipart/form-data">
<input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
<label>Source JSON file <input type="file" name="document"
accept=".json,application/json" required></label>
<button class="button button-primary" type="submit">Preview import</button></form>
<p><a href="/admin/sources">Back to sources</a></p>''',
        lan_mode=lan_mode,
    )


def render_source_import_preview(
    preview: SourceImportPreview, *, preview_id: str, csrf_token: str, lan_mode: bool
) -> bytes:
    esc = html.escape
    rows = []
    for index, row in enumerate(preview.rows):
        source = row.incoming
        if isinstance(source, UnsupportedPortableSource):
            issues = "".join(
                f"<li>{esc(issue.category)}: {esc(issue.reason)}</li>" for issue in source.issues
            )
            details = f"<ul>{issues}</ul>"
        else:
            definition = "".join(
                f"<dt>{esc(field.name)}</dt><dd>{esc(str(getattr(source, field.name)))}</dd>"
                for field in fields(SourceDefinition)
                if field.name not in {"id", "external_references"}
            )
            details = (
                f"<details><summary>Imported definition</summary><dl>{definition}</dl></details>"
            )
        differences = ""
        if row.local is not None and row.differences:
            changes = "".join(
                f"<li>{esc(field)}: incoming {esc(str(getattr(source, field)))}; "
                f"local {esc(str(getattr(row.local, field)))}</li>"
                for field in row.differences
            )
            differences = (
                f'<ul>{changes}</ul><label>Conflict decision <select name="decision_{index}">'
                '<option value="cancel">Cancel import</option>'
                '<option value="keep_local">Keep local configuration</option></select></label>'
            )
        rows.append(
            f"<tr><td>{esc(source.display_name)}<br><code>{esc(source.id)}</code>{details}</td>"
            f"<td><code>{esc(row.spotify_show_id)}</code></td>"
            f"<td>{esc(row.status)}{differences}</td><td><code>"
            f"{esc(row.local_id) if row.local_id is not None else 'Not available'}</code></td></tr>"
        )
    acknowledgement = ""
    if not preview.unsupported and preview.creates and preview.schema_version < 8:
        acknowledgement = """<p class="notice">Creating these sources adopts schema v8.
Older images cannot
read v8; rollback may require restoring a compatible pre-v8 backup. A portable JSON export is not
 a complete production backup.</p><label><input type="checkbox" name="ack_v8" value="1" required>
I understand the schema v8 compatibility and backup requirement.</label>"""
    if preview.unsupported:
        confirmation = (
            '<p class="notice">Apply is blocked: this document contains '
            f"{preview.unsupported} unsupported source(s). "
            "No sources from this batch can be installed. "
            "Use a compatible document or cancel the import.</p>"
        )
    else:
        confirmation = (
            acknowledgement
            + """<label>
<input type="checkbox" name="confirm" value="1" required>Confirm this import</label>
<p><button class="button button-primary" type="submit" name="action" value="apply">
Apply import</button></p>"""
        )
    return _page(
        f'''<h2>Review source import</h2>
<p>{preview.creates} source(s) to create. Nothing has been installed.</p>
<p>Spotify show ID determines identity. Imported names, logical IDs and provenance
never authorize overwriting local configuration.</p>
<form method="post" action="/admin/sources/import/apply">
<input type="hidden" name="csrf_token" value="{esc(csrf_token, quote=True)}">
<input type="hidden" name="preview_id" value="{esc(preview_id, quote=True)}">
<div class="table-wrap"><table><thead><tr><th scope="col">Imported source</th>
<th scope="col">Spotify show</th>
<th scope="col">Comparison</th><th scope="col">Local ID</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
{confirmation}</form>
<form method="post" action="/admin/sources/import/apply">
<input type="hidden" name="csrf_token" value="{esc(csrf_token, quote=True)}">
<input type="hidden" name="preview_id" value="{esc(preview_id, quote=True)}">
<button class="button" type="submit" name="action" value="cancel">Cancel import</button>
</form>''',
        lan_mode=lan_mode,
    )


def render_source_import_result(result: SourceImportResult | None, *, lan_mode: bool) -> bytes:
    if result is None:
        return _page(
            "<h2>Import cancelled</h2><p>No sources were installed.</p>"
            '<p><a href="/admin/sources">Back to sources</a></p>',
            lan_mode=lan_mode,
        )
    result_rows = []
    for row in result.rows:
        assert row.local_id is not None
        result_rows.append(
            f"<tr><td><code>{html.escape(row.incoming.id)}</code></td>"
            f"<td><code>{html.escape(row.local_id)}</code></td><td>{html.escape(row.status)}</td></tr>"
        )
    rows = "".join(result_rows)
    outcome = (
        "No configuration was written."
        if not result.created
        else f"{result.created} source(s) installed."
    )
    warning = (
        '<p class="notice">The save reported a durability error. '
        "Readback confirms the complete configuration is present; "
        "no second save was attempted. Verify storage health before restarting.</p>"
        if result.recovered
        else ""
    )
    return _page(
        f"""<h2>Source import complete</h2><p>{outcome}</p>{warning}
<p>Existing sources and playlist assignments are preserved.</p>
<table><thead><tr><th scope="col">Original ID</th><th scope="col">Local ID</th>
<th scope="col">Result</th></tr></thead><tbody>{rows}</tbody></table>
<p><a href="/admin/sources">Back to sources</a></p>""",
        lan_mode=lan_mode,
    )
