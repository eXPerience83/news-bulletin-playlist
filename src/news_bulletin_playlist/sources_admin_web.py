"""Sources administration rendering helpers."""

from __future__ import annotations

import html

from news_bulletin_playlist.admin_web import render_admin_shell, render_lan_warning
from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.engine import EngineCycleResult, SourceCycleOutcome
from news_bulletin_playlist.managed_admin import ManagedAdminSnapshot
from news_bulletin_playlist.managed_state import ManagedSource, ManagedState, effective_sources
from news_bulletin_playlist.models import (
    EditorialScope,
    SourceDefinition,
    SourceId,
    SourceOriginKind,
)
from news_bulletin_playlist.public_release_web import public_release_selector
from news_bulletin_playlist.source_admin import (
    MAX_SOURCE_AUDIT_BATCH_SOURCES,
    source_disable_blockers,
)
from news_bulletin_playlist.temporal_strategy_web import (
    TEMPORAL_STRATEGY_LABELS,
    temporal_strategy_selector,
)
from news_bulletin_playlist.web_ui import status_badge


def render_sources_page(
    *,
    snapshot: ManagedAdminSnapshot,
    state: ManagedState,
    catalog: BuiltInCatalog,
    last_cycle: EngineCycleResult | None,
    lan_mode: bool,
    csrf_token: str,
    notice: str | None = None,
) -> bytes:
    """Render effective sources, with lifecycle actions only for custom definitions."""
    safe_csrf = html.escape(csrf_token, quote=True)
    body = "".join(
        (
            render_lan_warning() if lan_mode else "",
            f'<div class="notice">{html.escape(notice)}</div>' if notice else "",
            f"""
<section class="section admin-section" aria-labelledby="source-catalog-heading">
  <div class="section-head">
    <div>
      <p class="eyebrow">Catalogue</p>
      <h2 id="source-catalog-heading">Broadcasters and sources</h2>
    </div>
    <p class="section-note">{len(catalog.sources)} built-in, {len(state.sources)} custom sources</p>
  </div>
  <p><a class="button button-primary" href="/admin/sources/new">Add source</a></p>
  <form method="post" action="/admin/sources/audits/export">
    <input type="hidden" name="csrf_token" value="{safe_csrf}">
    <button class="button" type="submit">Export all audits</button>
  </form>
  <p class="muted">Starts a single read-only audit in the background, up to
     {MAX_SOURCE_AUDIT_BATCH_SOURCES} sources. The progress page stays responsive, and
     the sanitized ZIP is available when complete. Only one batch may run at a time.</p>
  <p class="muted">Provider origin, editorial scope and language are separate; an institutional
     origin need not be a country.</p>
  {_sources_table(snapshot, catalog, state, last_cycle, csrf_token)}
</section>
""",
        )
    )
    return render_admin_shell(
        title="Sources",
        active_tab="sources",
        intro=(
            "Inspect the broadcasters and Spotify-first news sources available to playlist "
            "policies."
        ),
        body_html=body,
    )


def _sources_table(
    snapshot: ManagedAdminSnapshot,
    catalog: BuiltInCatalog,
    state: ManagedState,
    last_cycle: EngineCycleResult | None,
    csrf_token: str,
) -> str:
    source_outcomes = (
        {} if last_cycle is None else {outcome.source_id: outcome for outcome in last_cycle.sources}
    )
    sources = effective_sources(catalog, state)
    built_in_ids = {source.id for source in catalog.sources}
    used_by: dict[SourceId, list[str]] = {source.id: [] for source in sources}
    for playlist in snapshot.managed:
        for source_id in playlist.source_ids:
            used_by.setdefault(source_id, []).append(playlist.display_name)
    rows = []
    for source in sources:
        playlists = ", ".join(used_by.get(source.id, [])) or "Not used"
        show_id = next(
            reference.external_id
            for reference in source.external_references
            if reference.system == "spotify" and reference.resource_type == "show"
        )
        spotify_link = f"https://open.spotify.com/show/{show_id}"
        health = _source_health_badge(source_outcomes.get(source.id), enabled=source.enabled)
        ownership = "Built-in" if source.id in built_in_ids else "Custom"
        state_label = (
            "Image-owned"
            if source.id in built_in_ids
            else ("Enabled" if source.enabled else "Disabled")
        )
        controls = ""
        safe_id = html.escape(str(source.id), quote=True)
        safe_csrf = html.escape(csrf_token, quote=True)
        controls += (
            '<form method="post" action="/admin/sources/audit">'
            f'<input type="hidden" name="csrf_token" value="{safe_csrf}">'
            f'<input type="hidden" name="source_id" value="{safe_id}">'
            '<button class="button" type="submit">Audit</button></form>'
        )
        if source.id in built_in_ids:
            controls += (
                f'<a href="/admin/sources/edit?source_id={safe_id}">Edit public release time</a>'
            )
        if source.id not in built_in_ids:
            next_enabled = not source.enabled
            verb = "Enable" if next_enabled else "Disable"
            controls += f'<a href="/admin/sources/edit?source_id={safe_id}">Edit</a>'
            blocked = (
                source_disable_blockers(
                    source.id,
                    sources=sources,
                    playlists=snapshot.managed,
                    provisioning_intent=snapshot.provisioning_intent,
                )
                if source.enabled
                else ()
            )
            if blocked:
                controls += (
                    '<details class="source-action-disclosure">'
                    "<summary>Cannot disable</summary><p>Cannot disable: "
                    f"{html.escape(', '.join(blocked))} would have no enabled sources. "
                    "Add another enabled source or pause the active playlists first; "
                    "resolve any pending activation.</p></details>"
                )
            else:
                if not next_enabled and used_by.get(source.id):
                    controls += (
                        '<details class="source-action-disclosure">'
                        "<summary>Disable impact</summary><p>Disable affects: "
                        f"{html.escape(', '.join(used_by[source.id]))}. "
                        "This source stops contributing "
                        "episodes at the next reconciliation; its episodes may be removed from "
                        "Spotify. Playlists continue updating with their remaining enabled "
                        "sources. "
                        "Assignments are kept and return automatically on Enable.</p></details>"
                    )
                controls += (
                    '<form method="post" action="/admin/sources/set-enabled">'
                    f'<input type="hidden" name="csrf_token" value="{safe_csrf}">'
                    f'<input type="hidden" name="source_id" value="{safe_id}">'
                    f'<input type="hidden" name="enabled" value="{int(next_enabled)}">'
                    f'<button class="button" type="submit">{verb}</button></form>'
                )
            pending = (
                snapshot.provisioning_intent is not None
                and source.id in snapshot.provisioning_intent.source_ids
            )
            if used_by.get(source.id) or pending:
                controls += '<p class="muted">Unassign before deletion.</p>'
            else:
                controls += f'<a href="/admin/sources/delete?source_id={safe_id}">Delete</a>'
        rows.append(
            "<tr>"
            f"<td>{html.escape(source.display_name)} "
            f'<a href="{html.escape(spotify_link, quote=True)}" '
            'target="_blank" rel="noopener noreferrer">Spotify</a></td>'
            f"<td>{html.escape(str(source.origin))}</td>"
            f"<td><code>{html.escape(source.editorial_scope.value)}</code></td>"
            f"<td>{html.escape(', '.join(str(value) for value in source.languages))}</td>"
            f"<td>{health}</td>"
            f"<td>{html.escape(state_label)}<br>{TEMPORAL_STRATEGY_LABELS[source.temporal_strategy]}"
            f"<br>Public release: {source.public_release_mode.value}</td>"
            f"<td>{html.escape(playlists)}</td>"
            f"<td>{status_badge(ownership, tone='muted')}</td>"
            f'<td class="source-actions-cell"><div class="source-actions">{controls}</div></td>'
            "</tr>"
        )
    return (
        '<div class="card table-card"><div class="table-scroll">'
        "<table><thead><tr>"
        '<th scope="col">Source</th><th scope="col">Origin</th>'
        '<th scope="col">Scope</th><th scope="col">Language</th>'
        '<th scope="col">Health</th><th scope="col">State</th><th scope="col">Used by</th>'
        '<th scope="col">Ownership</th><th scope="col">Actions</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div></div>"
    )


def render_edit_source_page(
    source: ManagedSource,
    *,
    used_by: tuple[str, ...],
    csrf_token: str,
    lan_mode: bool,
) -> bytes:
    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    origins = "".join(
        f'<option value="{kind.value}"'
        f"{' selected' if kind is source.origin.kind else ''}>{kind.value.title()}</option>"
        for kind in SourceOriginKind
    )
    scopes = "".join(
        f'<option value="{scope.value}"'
        f"{' selected' if scope is source.editorial_scope else ''}>{scope.value}</option>"
        for scope in EditorialScope
    )
    body = (
        (render_lan_warning() if lan_mode else "")
        + f"""
<section class="section admin-section">
  <h2>Edit custom source</h2>
  <p>Source ID: <code>{esc(source.id)}</code></p>
  <p>Spotify show ID: <code>{esc(source.spotify_show_id)}</code>
     <a href="https://open.spotify.com/show/{esc(source.spotify_show_id)}"
        target="_blank" rel="noopener noreferrer">Open in Spotify</a></p>
  <p>Timezone: {esc(source.timezone)}; parser: {esc(source.parser_id)};
     filter: {esc(source.collection_filter_id or "none")};
     state: {"Enabled" if source.enabled else "Disabled"}.</p>
  <p>Used by: {esc(", ".join(used_by) or "Not used")}</p>
  <p class="muted">Show identity and timezone cannot be changed here.</p>
  <form method="post" action="/admin/sources/update">
    <input type="hidden" name="csrf_token" value="{esc(csrf_token)}">
    <input type="hidden" name="source_id" value="{esc(source.id)}">
    <label>Display name <input name="display_name" maxlength="120" required
           value="{esc(source.display_name)}"></label>
    <label>Origin kind <select name="origin_kind">{origins}</select></label>
    <label>Origin code <input name="origin_code" maxlength="12" required
           value="{esc(source.origin.code)}"></label>
    <label>Language <input name="language" required value="{esc(source.language)}"></label>
    <label>Editorial scope <select name="editorial_scope">{scopes}</select></label>
    {temporal_strategy_selector(source.temporal_strategy)}
    {public_release_selector(source.public_release_mode)}
    <p class="muted">Temporal changes apply at the next normal catalogue refresh.
       Saving does not force a Spotify request.</p>
    <button class="button button-primary" type="submit">Save source</button>
  </form>
</section>
"""
    )
    return render_admin_shell(
        title="Edit source",
        active_tab="sources",
        intro="Edit safe custom-source metadata.",
        body_html=body,
    )


def render_delete_source_page(
    source: ManagedSource,
    *,
    used_by: tuple[str, ...],
    pending: bool,
    csrf_token: str,
    lan_mode: bool,
) -> bytes:
    """Show current references, without treating this GET as deletion authorization."""
    safe_name = html.escape(source.display_name)
    safe_id = html.escape(str(source.id), quote=True)
    safe_show = html.escape(source.spotify_show_id, quote=True)
    references = html.escape(", ".join(used_by) or "Not used")
    pending_text = "Pending playlist activation" if pending else "None"
    blocked = bool(used_by) or pending
    action = (
        '<p class="warning">Unassign this source from every playlist and resolve pending '
        "activation before deletion.</p>"
        if blocked
        else f"""
<form method="post" action="/admin/sources/delete">
  <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
  <input type="hidden" name="source_id" value="{safe_id}">
  <button class="button button-danger" type="submit">Delete custom source</button>
</form>"""
    )
    body = (
        (render_lan_warning() if lan_mode else "")
        + f"""
<section class="section admin-section">
  <h2>Delete custom source</h2>
  <p>{safe_name} · <code>{safe_id}</code> · {"Enabled" if source.enabled else "Disabled"}</p>
  <p><a href="https://open.spotify.com/show/{safe_show}" target="_blank"
        rel="noopener noreferrer">Open Spotify show</a></p>
  <p>Used by: {references}. Pending activation: {pending_text}.</p>
  <p>This removes the installation-owned custom definition, current accepted catalogue,
     source refresh state and public detector state. It does not delete the Spotify show,
     Spotify playlists, playlist definitions, 30-day accepted-episode research history,
     diagnostic history or authorization.</p>
  {action}
</section>"""
    )
    return render_admin_shell(
        title="Delete source",
        active_tab="sources",
        intro="Confirm local source deletion.",
        body_html=body,
    )


def _source_health_badge(outcome: SourceCycleOutcome | None, *, enabled: bool = True) -> str:
    if not enabled:
        return status_badge("Disabled", tone="muted")
    if outcome is None:
        return status_badge("No cycle data", tone="muted")
    if outcome.ok:
        return status_badge("Healthy", tone="success")
    return status_badge("Failed", tone="danger")


def render_builtin_public_release_page(
    source: SourceDefinition, *, csrf_token: str, lan_mode: bool
) -> bytes:
    """Edit only the installation-owned public policy overlay of an image-owned source."""
    esc = html.escape
    body = (
        (render_lan_warning() if lan_mode else "")
        + f"""
<section class="section admin-section">
  <h2>Spotify public release time</h2>
  <p>{esc(source.display_name)}; timezone: {esc(str(source.timezone))}.</p>
  <p>Title time detection (read-only): {TEMPORAL_STRATEGY_LABELS[source.temporal_strategy]}.</p>
  <form method="post" action="/admin/sources/public-release-mode">
    <input type="hidden" name="csrf_token" value="{esc(csrf_token, quote=True)}">
    <input type="hidden" name="source_id" value="{esc(str(source.id), quote=True)}">
    {public_release_selector(source.public_release_mode)}
    <p>Changes apply at the next normal catalogue refresh. Saving makes no Spotify requests.
       Already accepted public instants are retained in history; Disabled suspends their use.</p>
    <button class="button button-primary" type="submit">Save source</button>
  </form>
</section>
"""
    )
    return render_admin_shell(
        title="Edit source",
        active_tab="sources",
        intro="Configure public release interpretation for this built-in source.",
        body_html=body,
    )
