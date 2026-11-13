"""Escaped HTML adapters for portable playlist preview and exact-second draft editing."""

from __future__ import annotations

import hashlib
import html
import json

from news_bulletin_playlist.admin_web import render_admin_shell
from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.playlist_import import (
    PlaylistImportPreview,
    PlaylistImportResult,
    PlaylistV2ImportPlan,
    PlaylistV2ImportResult,
)
from news_bulletin_playlist.portable_playlist_metadata import exception_payload
from news_bulletin_playlist.portable_playlists import canonical_json
from news_bulletin_playlist.source_import_web import render_source_import_rows


def upload_page(csrf: str, *, inspection_hold: bool = False) -> bytes:
    notice = (
        '<div class="notice" role="status">Startup inspection hold is active. '
        "Playlist imports and Spotify writes are locked.</div>"
        if inspection_hold
        else ""
    )
    disabled = " disabled" if inspection_hold else ""
    return render_admin_shell(
        title="Import playlist ZIP",
        active_tab="playlists",
        intro="Local draft import; no Spotify calls. Publishing later is a separate opt-in.",
        body_html=notice
        + f'''<h2>Import complete playlist ZIP v2</h2>
<p>Recommended for lossless owner-template policy, dormant exceptions and source closure.
Maximum 2 MiB; preview is read-only and never contacts Spotify.</p>
<form method="post" action="/admin/playlists/import/v2/preview" enctype="multipart/form-data">
<input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}">
<label>ZIP v2 package
<input type="file" name="document" accept=".zip,application/zip" required{disabled}>
</label>
<button class="button button-primary" type="submit"{disabled}>Preview ZIP v2</button></form>
<h2>Legacy playlist ZIP v1</h2>
<p>Maximum 1 MiB. Preview validates everything without writing files.</p>
<form method="post" action="/admin/playlists/import/preview" enctype="multipart/form-data">
<input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}">
<label>Playlist ZIP
<input type="file"
name="document" accept=".zip,application/zip"
required{disabled}>
</label>
<button class="button button-primary" type="submit"{disabled}>Preview import</button></form>''',
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
Publishing later requires explicit confirmation to create a NEW Spotify playlist.
The original Spotify destination is never restored automatically.</p>
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
        intro="Local drafts only; Spotify is unchanged until explicitly published.",
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


def preview_v2_page(plan: PlaylistV2ImportPlan, preview_id: str, csrf: str) -> bytes:
    """Review the whole v2 source/template closure before any local publication."""
    esc = html.escape
    playlist = plan.bundle.playlist
    source_rows = "".join(
        "<tr>"
        f"<td><code>{esc(str(getattr(row.incoming, 'id', 'unsupported')))}</code></td>"
        f"<td><code>{esc(row.spotify_show_id)}</code></td>"
        f"<td>{esc(row.status)}</td>"
        f"<td><code>{esc(str(row.local_id or ''))}</code></td>"
        f"<td>{esc(', '.join(row.differences) or 'none')}</td>"
        "</tr>"
        for row in plan.sources.rows
    )
    controls = ""
    if not plan.blocked:
        acknowledgement = ""
        if plan.sources.schema_version < 10:
            acknowledgement = """<p class="notice">This import promotes the local managed overlay
to schema v10. Older images cannot read it and ZIP does not restore full production
state. Confirm this forward-only compatibility change before importing.</p>
<label><input type="checkbox" name="ack_schema" value="1" required>
I accept the local schema v10 compatibility change.</label>"""
        controls = acknowledgement + """
<label><input type="checkbox" name="confirm" value="1" required>
I confirm installation of these local owner sources, full template and editable draft.
No Spotify destination will be created or linked.</label>
<button class="button button-primary" name="action" value="apply" type="submit">
Apply offline ZIP v2 import</button>"""
    fields = (
        f'<input type="hidden" name="csrf_token" value="{esc(csrf, quote=True)}">'
        f'<input type="hidden" name="preview_id" value="{esc(preview_id, quote=True)}">'
    )
    template = plan.bundle.template
    assert template is not None
    attribution = plan.bundle.template_attribution
    proposed_id = esc(str(plan.owner_template.id)) if plan.owner_template else "blocked"
    body = f"""<h2>{esc(playlist["display_name"])}</h2>
<p>ZIP v2 · UUID <code>{esc(playlist["portable_id"])}</code></p>
<p><strong>Preview:</strong> {esc(plan.reason)}</p>
<p>{plan.sources.creates} source(s) will be installed. Other identical owner sources
may be reused. Differing capabilities or unowned overlaps block the whole import.</p>
<p>The current playlist settings become an editable local draft. The full separate
owner template, including unassigned sources and dormant duration exceptions,
will be retained. No Spotify destination ID, OAuth or episodes are imported.</p>
<div class="table-wrap"><table>
<thead><tr><th scope="col">Incoming ID</th><th scope="col">Spotify show</th>
<th scope="col">Decision</th><th scope="col">Local ID</th>
<th scope="col">Differences</th></tr></thead>
<tbody>{source_rows}</tbody></table></div>
<p>Current selected source IDs:
<code>{esc(", ".join(map(str, plan.selected_source_ids or ())))}</code></p>
<p>Owner template: <code>{proposed_id}</code>.
{len(template.default_source_ids)} default sources and
{len(template.duration_policy.exceptions)} complete duration exceptions.</p>
<details><summary>Exact incoming playlist configuration</summary>
<pre>{esc(canonical_json(playlist).decode("utf-8"))}</pre></details>
<details><summary>Complete owner template, including dormant selectors and DST fold</summary>
<pre>{esc(repr(template))}</pre></details>
<details><summary>Original template artwork attribution</summary>
<pre>{esc(canonical_json(attribution).decode("utf-8"))}</pre></details>
<form method="post" action="/admin/playlists/import/v2/apply">
{fields}{controls}</form>
<form method="post" action="/admin/playlists/import/v2/apply">
{fields}<button class="button" name="action" value="cancel" type="submit">
Cancel import</button></form>"""
    return render_admin_shell(
        title="Review complete ZIP v2 import",
        active_tab="playlists",
        intro="Read-only comparison; apply is a separate local-only confirmation.",
        body_html=body,
    )


def result_v2_page(result: PlaylistV2ImportResult | None) -> bytes:
    if result is None:
        body = "<h2>ZIP v2 import cancelled</h2><p>No configuration was installed.</p>"
    else:
        warning = (
            '<p class="notice">A durability error occurred after publication; '
            "readback confirms the complete owner inventory and current draft. "
            "No blind retry was performed. Inspect storage health.</p>"
            if result.recovered
            else ""
        )
        body = (
            "<h2>ZIP v2 offline import complete</h2>"
            f"<p>Owner template: <code>{html.escape(result.owner_template_id)}</code>. "
            f"Current draft: <code>{html.escape(result.current_draft_id)}</code>. "
            f"{result.created_sources} new sources.</p>"
            + warning
            + "<p>Spotify was not contacted, created, linked or enabled. "
            "Review the local draft before any separate activation.</p>"
            + '<p><a href="/admin/playlists/imported/edit?id='
            + html.escape(result.current_draft_id, quote=True)
            + '">Edit local draft</a></p>'
        )
    return render_admin_shell(
        title="ZIP v2 import result",
        active_tab="playlists",
        intro="Owner configuration was imported offline; Spotify is unchanged.",
        body_html=body + '<p><a href="/admin/">Back to playlists</a></p>',
    )
