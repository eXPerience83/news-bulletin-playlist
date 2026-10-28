"""Escaped HTML adapters for portable playlist preview and exact-second draft editing."""

from __future__ import annotations

import hashlib
import html
import json

from news_bulletin_playlist.admin_web import render_admin_shell
from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.playlist_import import PlaylistImportPreview, PlaylistImportResult
from news_bulletin_playlist.portable_playlist_metadata import exception_payload
from news_bulletin_playlist.portable_playlists import canonical_json
from news_bulletin_playlist.source_import_web import render_source_import_rows


def upload_page(csrf: str) -> bytes:
    return render_admin_shell(
        title="Import playlist ZIP",
        active_tab="playlists",
        intro="Local draft import; no Spotify calls or activation.",
        body_html=f'''<h2>Import portable playlist ZIP v1</h2>
<p>Maximum 1 MiB. Preview validates everything without writing files.</p>
<form method="post" action="/admin/playlists/import/preview" enctype="multipart/form-data">
<input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}">
<label>Playlist ZIP
<input type="file"
name="document" accept=".zip,application/zip"
required>
</label>
<button class="button button-primary" type="submit">Preview import</button></form>''',
    )


def preview_page(preview: PlaylistImportPreview, preview_id: str, csrf: str) -> bytes:
    esc = html.escape
    p = preview.bundle.playlist
    details = esc(canonical_json(p).decode("utf-8"))
    ack = ""
    if preview.status == "create" and preview.sources.schema_version < 10:
        ack = """<p class="notice">This import explicitly adopts managed-state v10
(including v9 UUID identities).
Older images, including v9, cannot read v10.
Rollback may require restoring a compatible complete backup.
A portable ZIP is not a production backup.</p>
<label><input type="checkbox" name="ack_schema" value="1" required>
I accept the v10 compatibility and backup requirement.</label>"""
    confirm = (
        ""
        if preview.blocked
        else ack
        + """<label><input type="checkbox" name="confirm" value="1" required>
Confirm import as a local draft only</label>
<button class="button button-primary"
name="action"
value="apply" type="submit">Apply import</button>"""
    )
    fields = (
        f'<input type="hidden" name="csrf_token" value="{esc(csrf, quote=True)}">'
        f'<input type="hidden" name="preview_id" value="{esc(preview_id, quote=True)}">'
    )
    body = f"""<h2>{esc(p["display_name"])}</h2><p>UUID: <code>{esc(p["portable_id"])}</code></p>
<p>Result: {esc(preview.status)}. {esc(preview.reason)}</p>
<p>JPEG verified
byte for byte; a durable content-addressed copy will be prepared before the state commit.
Imported drafts retain exact seconds, exceptions and provenance.
Their Spotify activation is blocked.</p>
<details>
<summary>Complete imported playlist configuration and attribution</summary>
<pre>{details}</pre></details>
<p>Source decisions below show the effective local configuration when Keep local is selected.
UUID comparison is calculated using those local IDs and configurations.</p>
<form method="post" action="/admin/playlists/import/apply">{fields}
<div class="table-wrap">
<table><thead><tr><th scope="col">Imported source</th><th scope="col">Show</th>
<th scope="col">Comparison</th><th scope="col">Local ID</th></tr></thead>
<tbody>{render_source_import_rows(preview.sources)}</tbody></table></div>{confirm}</form>
<form method="post" action="/admin/playlists/import/apply">{fields}
<button class="button" name="action" value="cancel" type="submit">Cancel import</button></form>"""
    return render_admin_shell(
        title="Review playlist import",
        active_tab="playlists",
        intro="Preview is read-only. Existing configuration is never overwritten.",
        body_html=body,
    )


def result_page(result: PlaylistImportResult | None) -> bytes:
    if result is None:
        body = "<h2>Import cancelled</h2><p>No configuration was installed.</p>"
    else:
        message = (
            f"Created local draft {html.escape(result.local_id)} "
            f"and {result.created_sources} sources."
            if result.created_draft
            else f"Reused {html.escape(result.local_id)}. No configuration or artwork was written."
        )
        warning = (
            '<p class="notice">Save reported a durability error; '
            "readback confirms the complete state is present. No retry was attempted. "
            "Check storage health.</p>"
            if result.recovered
            else ""
        )
        body = (
            f"<h2>Playlist import complete</h2><p>{message}</p>{warning}"
            '<p><a class="button" href="/admin/playlists/imported/edit?id='
            f'{html.escape(result.local_id, quote=True)}">Edit local draft</a></p>'
        )
    return render_admin_shell(
        title="Playlist import result",
        active_tab="playlists",
        intro="Local drafts only; Spotify is unchanged.",
        body_html=body + '<p><a href="/admin/">Back to playlists</a></p>',
    )


def imported_draft_editor(draft: LocalPlaylistTemplate, csrf: str) -> str:
    esc = html.escape
    fields = "".join(
        f'<input type="hidden" name="{name}" value="{esc(value, quote=True)}">'
        for name, value in {
            "csrf_token": csrf,
            "template_id": str(draft.id),
            "fingerprint": hashlib.sha256(repr(draft).encode()).hexdigest(),
        }.items()
    )
    exceptions = json.dumps(
        [exception_payload(e) for e in draft.duration_exceptions], ensure_ascii=False, indent=2
    )
    return f'''<form method="post" action="/admin/playlists/imported/update">{fields}
<label>Name
<textarea name="display_name" maxlength="4096" required>
{esc(draft.display_name)}</textarea></label>
<label>Description
<textarea
name="description"
maxlength="65536">
{esc(draft.description)}</textarea>
</label>
<label>Ordered source IDs · one per line
<textarea
name="source_ids"
required>{esc(chr(10).join(draft.default_source_ids))}</textarea>
</label>
<label>Countries · comma separated
<input
name="countries"
value="{esc(",".join(draft.countries), quote=True)}">
</label>
<label>Languages · comma separated
<input
name="languages"
value="{esc(",".join(draft.languages), quote=True)}">
</label>
<label>Retention hours
<input type="number"
name="retention_hours" min="1"
max="9223372036854775807"
value="{draft.retention_hours}"
required>
</label>
<label>Maximum episodes
<input type="number"
name="max_episodes" min="1"
max="9223372036854775807"
value="{draft.max_episodes}"
required>
</label>
<label>Minimum seconds
<input type="number"
name="min_seconds" min="0"
max="9223372036854775807"
value="{draft.min_duration_seconds}"
required>
</label>
<label>Maximum seconds
<input type="number"
name="max_seconds" min="1"
max="9223372036854775807"
value="{draft.max_duration_seconds}"
required>
</label>
<label>Duration exceptions · strict JSON
<textarea
name="exceptions">{esc(exceptions)}</textarea>
</label>
<input type="hidden" name="cover_id" value="{esc(draft.cover_id, quote=True)}">
<label>
<input type="checkbox"
name="hidden"
value="1"{" checked" if draft.hidden else ""}>Hide draft</label>
<p>Exact seconds and exception selectors are preserved. Removing a referenced source requires
explicitly editing its exceptions.
Use the existing cover upload to replace artwork.
Imported origin observations and attribution remain recorded below.</p>
<details>
<summary>Original provenance (inert, never activation instructions)</summary>
<pre>{esc(draft.portable_metadata or "")}</pre></details>
<button class="button" type="submit">Save imported draft</button></form>'''
