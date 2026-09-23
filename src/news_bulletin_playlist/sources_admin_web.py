"""Sources administration rendering helpers."""

from __future__ import annotations

import html

from news_bulletin_playlist.admin_web import render_admin_shell, render_lan_warning
from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.engine import EngineCycleResult, SourceCycleOutcome
from news_bulletin_playlist.managed_admin import ManagedAdminSnapshot
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.web_ui import status_badge


def render_sources_page(
    *,
    snapshot: ManagedAdminSnapshot,
    catalog: BuiltInCatalog,
    last_cycle: EngineCycleResult | None,
    lan_mode: bool,
) -> bytes:
    """Render the built-in source catalogue as a focused read-only admin page."""

    body = "".join(
        (
            render_lan_warning() if lan_mode else "",
            """
<div class="notice">
  Source management is read-only in this step. Built-in sources remain image-owned; custom source
  add, disable and remove workflows are tracked separately.
</div>
""",
            f"""
<section class="section admin-section" aria-labelledby="source-catalog-heading">
  <div class="section-head">
    <div>
      <p class="eyebrow">Catalogue</p>
      <h2 id="source-catalog-heading">Broadcasters and sources</h2>
    </div>
    <p class="section-note">{len(catalog.sources)} built-in sources</p>
  </div>
  <p class="muted">Country/origin, editorial scope and language are separate so a foreign
     provider can still be useful to a global playlist.</p>
  {_sources_table(snapshot, catalog, last_cycle)}
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
    last_cycle: EngineCycleResult | None,
) -> str:
    source_outcomes = (
        {} if last_cycle is None else {outcome.source_id: outcome for outcome in last_cycle.sources}
    )
    used_by: dict[SourceId, list[str]] = {source.id: [] for source in catalog.sources}
    for playlist in snapshot.managed:
        for source_id in playlist.source_ids:
            used_by.setdefault(source_id, []).append(playlist.display_name)
    rows = []
    for source in catalog.sources:
        playlists = ", ".join(used_by.get(source.id, [])) or "Not used"
        rows.append(
            "<tr>"
            f"<td>{html.escape(source.display_name)}</td>"
            f"<td>{html.escape(', '.join(str(value) for value in source.countries))}</td>"
            f"<td><code>{html.escape(source.editorial_scope.value)}</code></td>"
            f"<td>{html.escape(', '.join(str(value) for value in source.languages))}</td>"
            f"<td>{_source_health_badge(source_outcomes.get(source.id))}</td>"
            f"<td>{html.escape(playlists)}</td>"
            f"<td>{status_badge('Built-in', tone='muted')}</td>"
            "</tr>"
        )
    return (
        '<div class="card table-card"><div class="table-scroll">'
        "<table><thead><tr>"
        '<th scope="col">Source</th><th scope="col">Origin</th>'
        '<th scope="col">Scope</th><th scope="col">Language</th>'
        '<th scope="col">Health</th><th scope="col">Used by</th>'
        '<th scope="col">Ownership</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div></div>"
    )


def _source_health_badge(outcome: SourceCycleOutcome | None) -> str:
    if outcome is None:
        return status_badge("No cycle data", tone="muted")
    if outcome.ok:
        return status_badge("Healthy", tone="success")
    return status_badge("Failed", tone="danger")
