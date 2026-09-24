"""Sources administration rendering helpers."""

from __future__ import annotations

import html

from news_bulletin_playlist.admin_web import render_admin_shell, render_lan_warning
from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.engine import EngineCycleResult, SourceCycleOutcome
from news_bulletin_playlist.managed_admin import ManagedAdminSnapshot
from news_bulletin_playlist.managed_state import ManagedState, effective_sources
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.web_ui import status_badge


def render_sources_page(
    *,
    snapshot: ManagedAdminSnapshot,
    state: ManagedState,
    catalog: BuiltInCatalog,
    last_cycle: EngineCycleResult | None,
    lan_mode: bool,
    notice: str | None = None,
) -> bytes:
    """Render built-in and installation-owned sources without management controls."""

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
  <p class="muted">Provider origin, editorial scope and language are separate; an institutional
     origin need not be a country.</p>
  {_sources_table(snapshot, catalog, state, last_cycle)}
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
        rows.append(
            "<tr>"
            f"<td>{html.escape(source.display_name)} "
            f'<a href="{html.escape(spotify_link, quote=True)}" '
            'target="_blank" rel="noopener noreferrer">Spotify</a></td>'
            f"<td>{html.escape(str(source.origin))}</td>"
            f"<td><code>{html.escape(source.editorial_scope.value)}</code></td>"
            f"<td>{html.escape(', '.join(str(value) for value in source.languages))}</td>"
            f"<td>{health}</td>"
            f"<td>{html.escape(state_label)}</td>"
            f"<td>{html.escape(playlists)}</td>"
            f"<td>{status_badge(ownership, tone='muted')}</td>"
            "</tr>"
        )
    return (
        '<div class="card table-card"><div class="table-scroll">'
        "<table><thead><tr>"
        '<th scope="col">Source</th><th scope="col">Origin</th>'
        '<th scope="col">Scope</th><th scope="col">Language</th>'
        '<th scope="col">Health</th><th scope="col">State</th><th scope="col">Used by</th>'
        '<th scope="col">Ownership</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div></div>"
    )


def _source_health_badge(outcome: SourceCycleOutcome | None, *, enabled: bool = True) -> str:
    if not enabled:
        return status_badge("Disabled", tone="muted")
    if outcome is None:
        return status_badge("No cycle data", tone="muted")
    if outcome.ok:
        return status_badge("Healthy", tone="success")
    return status_badge("Failed", tone="danger")
