"""Shared server-rendered administration pages for the 0.2.x Web UI."""

from __future__ import annotations

import html
import urllib.parse
from datetime import tzinfo
from typing import Literal

from news_bulletin_playlist import diagnostics_web
from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.diagnostics import MAX_DIAGNOSTIC_QUERY_LIMIT, DiagnosticEvent
from news_bulletin_playlist.engine import EngineCycleResult
from news_bulletin_playlist.managed_admin import (
    MAX_PLAYLIST_DESCRIPTION_LENGTH,
    MAX_PLAYLIST_NAME_LENGTH,
    ManagedAdminSnapshot,
)
from news_bulletin_playlist.managed_provisioning import ProvisioningState
from news_bulletin_playlist.managed_state import ManagedPlaylist
from news_bulletin_playlist.models import SourceDefinition, SourceId
from news_bulletin_playlist.spotify.auth import AuthorizationState
from news_bulletin_playlist.spotify.authorization_display import authorization_state_label
from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetricsSnapshot
from news_bulletin_playlist.web_ui import WEB_UI_STYLES, status_badge

AdminTab = Literal["playlists", "sources", "diagnostics"]

_ADMIN_UI_STYLES = """
.admin-shell {
  width: min(1240px, calc(100% - 2rem));
  min-width: 0;
  margin: 0 auto;
  padding: 1.1rem 0 4rem;
}

.admin-shell main {
  min-width: 0;
}

.admin-topbar {
  display: flex;
  min-height: 4rem;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.admin-topbar-actions,
.page-actions,
.form-actions {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  flex-wrap: wrap;
}

.admin-nav {
  display: flex;
  width: fit-content;
  max-width: 100%;
  gap: 0.35rem;
  margin: 1rem 0 0;
  padding: 0.35rem;
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
}

.admin-tab {
  display: inline-flex;
  min-height: 2.65rem;
  align-items: center;
  padding: 0.55rem 0.95rem;
  border-radius: 999px;
  color: var(--text-muted);
  font-size: 0.9rem;
  font-weight: 760;
  text-decoration: none;
  white-space: nowrap;
}

.admin-tab:hover {
  color: var(--text);
  background: var(--surface-hover);
}

.admin-tab.is-active {
  color: var(--text);
  background: var(--surface-raised);
  box-shadow: inset 0 0 0 1px var(--border-strong);
}

.page-head {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 2rem;
  padding: clamp(2.3rem, 6vw, 4.5rem) 0 1.2rem;
}

.page-head h1 {
  margin: 0;
  font-size: clamp(2.25rem, 5vw, 4.2rem);
  line-height: 0.98;
  letter-spacing: -0.055em;
}

.page-intro {
  max-width: 50rem;
  margin: 0.8rem 0 0;
  color: var(--text-muted);
}

.admin-section {
  min-width: 0;
  margin-top: 2rem;
}

.admin-section-head {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.85rem;
}

.admin-section h2,
.admin-panel h2 {
  margin: 0;
  font-size: clamp(1.3rem, 3vw, 1.85rem);
  letter-spacing: -0.03em;
}

.admin-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr));
  gap: 0.9rem;
}

.playlist-card,
.admin-panel {
  min-width: 0;
  padding: 1rem;
}

.card-head {
  display: flex;
  align-items: flex-start;
  gap: 0.9rem;
}

.card-head h3 {
  margin: 0;
  font-size: 1.15rem;
  letter-spacing: -0.02em;
}

.card-head p {
  margin: 0.38rem 0 0;
}

.cover {
  width: 5rem;
  height: 5rem;
  flex: 0 0 auto;
  object-fit: cover;
  border: 1px solid var(--border);
  border-radius: 0.8rem;
  background: var(--bg-soft);
}

form {
  margin: 0;
}

.playlist-card form {
  margin-top: 1rem;
}

label {
  display: block;
  margin-top: 0.75rem;
  color: var(--text);
  font-size: 0.88rem;
  font-weight: 720;
}

input[type="text"],
input[type="number"],
select,
textarea {
  width: 100%;
  margin-top: 0.32rem;
  padding: 0.62rem 0.7rem;
  border: 1px solid var(--border-strong);
  border-radius: 0.65rem;
  background: var(--bg-soft);
  color: var(--text);
  font: inherit;
}

textarea {
  min-height: 6rem;
  resize: vertical;
}

.duration-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 0.65rem;
}

fieldset {
  margin: 1rem 0 0;
  padding: 0.8rem;
  border: 1px solid var(--border);
  border-radius: 0.75rem;
}

legend {
  padding: 0 0.3rem;
  color: var(--text-muted);
  font-size: 0.82rem;
  font-weight: 760;
}

fieldset label {
  margin: 0.38rem 0;
  font-weight: 520;
}

button,
.button-secondary {
  display: inline-flex;
  min-height: 2.65rem;
  align-items: center;
  justify-content: center;
  padding: 0.62rem 0.95rem;
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  background: var(--surface-raised);
  color: var(--text);
  font: inherit;
  font-weight: 750;
  text-decoration: none;
  cursor: pointer;
}

button:hover,
.button-secondary:hover {
  background: var(--surface-hover);
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.52;
}

.danger {
  border-color: color-mix(in srgb, var(--danger) 50%, transparent);
  background: var(--danger-soft);
  color: var(--danger);
}

.warning,
.notice,
.error {
  margin: 1rem 0 0;
  padding: 0.82rem 0.95rem;
  border: 1px solid var(--border);
  border-radius: 0.8rem;
  background: var(--surface);
}

.warning {
  border-color: color-mix(in srgb, var(--warning) 35%, transparent);
  background: var(--warning-soft);
}

.notice {
  border-color: color-mix(in srgb, var(--success) 30%, transparent);
  background: var(--success-soft);
}

.error {
  border-color: color-mix(in srgb, var(--danger) 35%, transparent);
  background: var(--danger-soft);
}

.muted,
.empty {
  color: var(--text-muted);
}

.table-card {
  min-width: 0;
  overflow: hidden;
}

.table-scroll {
  max-width: 100%;
  overflow-x: auto;
}

.filter-form {
  display: grid;
  grid-template-columns: repeat(5, minmax(8rem, 1fr)) auto;
  gap: 0.7rem;
  align-items: end;
}

.filter-form label {
  margin-top: 0;
}

.admin-footer {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  margin-top: 2.7rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 0.8rem;
}

@media (max-width: 900px) {
  .page-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .filter-form {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 620px) {
  .admin-shell {
    width: min(100% - 1.15rem, 1240px);
  }

  .admin-topbar {
    align-items: flex-start;
  }

  .admin-topbar .brand span:last-child {
    display: none;
  }

  .page-head {
    padding-top: 2rem;
  }

  .filter-form,
  .duration-row {
    grid-template-columns: 1fr;
  }

  .admin-footer {
    flex-direction: column;
  }
}
"""


def render_admin_shell(
    *,
    title: str,
    active_tab: AdminTab,
    intro: str,
    body_html: str,
    actions_html: str = "",
) -> bytes:
    """Render one authenticated admin page using the project-wide design tokens."""

    tabs: tuple[tuple[AdminTab, str, str], ...] = (
        ("playlists", "Playlists", "/admin/"),
        ("sources", "Sources", "/admin/sources"),
        ("diagnostics", "Diagnostics", "/admin/diagnostics"),
    )
    navigation = "".join(
        _admin_tab(key=key, label=label, href=href, active=key == active_tab)
        for key, label, href in tabs
    )
    actions = f'<div class="page-actions">{actions_html}</div>' if actions_html else ""
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>{html.escape(title)} · News Bulletin Playlists</title>
  <style>{WEB_UI_STYLES}{_ADMIN_UI_STYLES}</style>
</head>
<body>
  <div class="admin-shell">
    <header>
      <div class="admin-topbar">
        <a class="brand" href="/admin/" aria-label="News Bulletin Playlists administration">
          <span class="brand-mark" aria-hidden="true">NB</span>
          <span>News Bulletin Playlists</span>
        </a>
        <div class="admin-topbar-actions">
          <a class="button-secondary" href="/">Status</a>
        </div>
      </div>
      <nav class="admin-nav" aria-label="Administration">{navigation}</nav>
    </header>
    <main id="main-content">
      <section class="page-head" aria-labelledby="page-title">
        <div>
          <p class="eyebrow">Administration</p>
          <h1 id="page-title">{html.escape(title)}</h1>
          <p class="page-intro">{html.escape(intro)}</p>
        </div>
        {actions}
      </section>
      {body_html}
    </main>
    <footer class="admin-footer">
      <span>Authenticated operator area</span>
      <a href="/">Return to read-only status</a>
    </footer>
  </div>
</body>
</html>
"""
    return document.encode("utf-8")


def render_playlists_page(
    *,
    snapshot: ManagedAdminSnapshot,
    catalog: BuiltInCatalog,
    spotify_state: AuthorizationState | None,
    csrf_token: str,
    last_cycle: EngineCycleResult | None,
    lan_mode: bool,
    notice: str | None = None,
    error: str | None = None,
) -> bytes:
    """Render playlist policy and lifecycle controls in the shared admin shell."""

    spotify_connected = spotify_state is AuthorizationState.CONNECTED
    spotify_label = authorization_state_label(spotify_state)
    csrf = html.escape(csrf_token, quote=True)
    managed = (
        "".join(
            _managed_card(
                playlist,
                catalog=catalog,
                csrf_token=csrf,
                last_cycle=last_cycle,
            )
            for playlist in snapshot.managed
        )
        or '<p class="empty">No managed playlists yet.</p>'
    )
    if snapshot.provisioning_intent is not None:
        available = (
            '<p class="empty">Resolve the pending playlist creation before activating another '
            "template.</p>"
        )
    else:
        available = (
            "".join(
                _template_card(
                    template,
                    catalog=catalog,
                    csrf_token=csrf,
                    spotify_connected=spotify_connected,
                )
                for template in snapshot.available_templates
            )
            or '<p class="empty">No additional built-in playlists are available.</p>'
        )
    spotify_action = "Reconnect Spotify" if spotify_connected else "Connect Spotify"
    actions = f"""
<form method="post" action="/admin/spotify/connect">
  <input type="hidden" name="csrf_token" value="{csrf}">
  <button type="submit">{html.escape(spotify_action)}</button>
</form>
"""
    body = "".join(
        (
            _lan_warning() if lan_mode else "",
            _message("notice", notice),
            _message("error", error),
            _provisioning_recovery(snapshot.provisioning_intent, csrf_token=csrf),
            f"""
<section class="admin-section" aria-labelledby="active-playlists-heading">
  <div class="admin-section-head">
    <div>
      <p class="eyebrow">Published</p>
      <h2 id="active-playlists-heading">Active playlists</h2>
    </div>
    <p class="section-note">Spotify authorization: {html.escape(spotify_label)}</p>
  </div>
  <p class="muted">Each playlist keeps its own source selection and duration ceiling. A source
     shared by several playlists is still fetched only once per engine cycle.</p>
  <div class="admin-grid">{managed}</div>
</section>
<section class="admin-section" aria-labelledby="available-playlists-heading">
  <div class="admin-section-head">
    <div>
      <p class="eyebrow">Templates</p>
      <h2 id="available-playlists-heading">Available playlists</h2>
    </div>
  </div>
  <p class="muted">Review the built-in defaults before creating a Spotify playlist.</p>
  <div class="admin-grid">{available}</div>
</section>
""",
        )
    )
    return render_admin_shell(
        title="Playlists",
        active_tab="playlists",
        intro=(
            "Manage which playlists are published, which sources feed each one and the current "
            "duration policy."
        ),
        body_html=body,
        actions_html=actions,
    )


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
            _lan_warning() if lan_mode else "",
            """
<div class="notice">
  Source management is read-only in this step. Built-in sources remain image-owned; custom source
  add, disable and remove workflows are tracked separately.
</div>
""",
            f"""
<section class="admin-section" aria-labelledby="source-catalog-heading">
  <div class="admin-section-head">
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


def render_admin_diagnostics_page(
    *,
    events: tuple[DiagnosticEvent, ...],
    filters: diagnostics_web.DiagnosticFilters,
    display_timezone: tzinfo,
    timezone_label: str,
    spotify_metrics: SpotifyRequestMetricsSnapshot | None,
) -> bytes:
    """Render diagnostics unchanged semantically inside the shared admin shell."""

    rows = "".join(
        diagnostics_web._event_row(event, display_timezone=display_timezone)  # noqa: SLF001
        for event in events
    )
    if not rows:
        rows = '<tr><td colspan="8">No diagnostic events match these filters.</td></tr>'
    export_href = "/admin/diagnostics/export.zip?" + filters.query_string(include_limit=False)
    severity_value = "" if filters.severity is None else filters.severity.value
    source_value = "" if filters.source_id is None else filters.source_id
    playlist_value = "" if filters.playlist_id is None else filters.playlist_id
    hours_value = "all" if filters.hours is None else str(filters.hours)
    metrics = diagnostics_web._spotify_metrics_section(spotify_metrics)  # noqa: SLF001
    body = f"""
<section class="admin-section card admin-panel" aria-labelledby="diagnostic-filters-heading">
  <h2 id="diagnostic-filters-heading">Filters</h2>
  <form class="filter-form" method="get" action="/admin/diagnostics">
    <label>Severity
      <select name="severity">{diagnostics_web._severity_options(severity_value)}</select>
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
      <select name="hours">{diagnostics_web._hours_options(hours_value)}</select>
    </label>
    <label>Rows
      <input type="number" name="limit" min="1" max="{MAX_DIAGNOSTIC_QUERY_LIMIT}"
             value="{filters.limit}">
    </label>
    <button type="submit">Apply filters</button>
  </form>
  <p class="muted">UI limit={MAX_DIAGNOSTIC_QUERY_LIMIT}; ZIP export uses a separate bounded
     capacity. Raw SQLite, OAuth material, authenticated provider response bodies and environment
     variables are never exposed here. A bounded response from an unauthenticated public Spotify
     schema failure may be retained only as a separate file in the authenticated ZIP.</p>
  <p><a class="button-secondary" href="{html.escape(export_href, quote=True)}">Download sanitized
     diagnostics ZIP</a></p>
</section>
<section class="admin-section card admin-panel">
  {metrics}
</section>
<section class="admin-section" aria-labelledby="diagnostic-events-heading">
  <div class="admin-section-head">
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


def _admin_tab(*, key: AdminTab, label: str, href: str, active: bool) -> str:
    del key
    active_class = " is-active" if active else ""
    current = ' aria-current="page"' if active else ""
    return (
        f'<a class="admin-tab{active_class}" href="{html.escape(href, quote=True)}"{current}>'
        f"{html.escape(label)}</a>"
    )


def _managed_card(
    playlist: ManagedPlaylist,
    *,
    catalog: BuiltInCatalog,
    csrf_token: str,
    last_cycle: EngineCycleResult | None,
) -> str:
    outcome = None
    if last_cycle is not None:
        outcome = next(
            (item for item in last_cycle.playlists if item.playlist_id == playlist.id),
            None,
        )
    if outcome is None:
        result = status_badge("No cycle data", tone="muted")
    elif outcome.ok:
        result = status_badge("Success", tone="success")
    else:
        result = status_badge("Failed — see Diagnostics", tone="danger")
    checked = " checked" if playlist.enabled else ""
    spotify_url = "https://open.spotify.com/playlist/" + urllib.parse.quote(
        playlist.destination.external_id,
        safe="",
    )
    source_controls = _source_checkboxes(
        catalog,
        selected=set(playlist.source_ids),
        prefix=f"edit-{playlist.id}",
    )
    template = catalog.playlist(playlist.template_id)
    effective_duration_seconds = (
        template.duration_policy.default_max_seconds
        if playlist.max_duration_seconds is None
        else playlist.max_duration_seconds
    )
    duration_minutes, duration_remainder = divmod(effective_duration_seconds, 60)
    state = (
        status_badge("Active", tone="success")
        if playlist.enabled
        else status_badge("Paused", tone="warning")
    )
    description = html.escape(playlist.description)
    return f"""
<article class="card playlist-card">
  <div class="card-head">
    {_cover_image(playlist.cover_id, playlist.display_name)}
    <div>
      <h3>{html.escape(playlist.display_name)}</h3>
      <p>{state} {result}</p>
      <p><a href="{html.escape(spotify_url, quote=True)}" target="_blank"
            rel="noopener noreferrer">Open in Spotify</a></p>
      <p class="muted">Spotify metadata/cover sync is independent from bulletin policy changes.</p>
    </div>
  </div>
  <form method="post" action="/admin/playlists/update">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <input type="hidden" name="playlist_id" value="{html.escape(str(playlist.id), quote=True)}">
    <input type="hidden" name="cover_id" value="{html.escape(playlist.cover_id, quote=True)}">
    <label>Name
      <input type="text" name="display_name" required maxlength="{MAX_PLAYLIST_NAME_LENGTH}"
             value="{html.escape(playlist.display_name, quote=True)}">
    </label>
    <label>Description
      <textarea name="description"
                maxlength="{MAX_PLAYLIST_DESCRIPTION_LENGTH}">{description}</textarea>
    </label>
    <div class="duration-row">
      <label>Maximum duration · minutes
        <input type="number" name="max_duration_minutes" required min="0" step="1"
               value="{duration_minutes}">
      </label>
      <label>Seconds
        <input type="number" name="max_duration_seconds_remainder" required min="0" max="59"
               step="1" value="{duration_remainder}">
      </label>
    </div>
    <p class="muted">Episodes longer than this are omitted from this playlist only.</p>
    <fieldset><legend>Sources</legend>{source_controls}</fieldset>
    <label><input type="checkbox" name="enabled" value="1"{checked}> Active</label>
    <button type="submit">Save playlist</button>
  </form>
  <form method="post" action="/admin/playlists/sync">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <input type="hidden" name="playlist_id" value="{html.escape(str(playlist.id), quote=True)}">
    <button type="submit">Apply Spotify metadata &amp; cover</button>
  </form>
  <form method="post" action="/admin/playlists/stop">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <input type="hidden" name="playlist_id" value="{html.escape(str(playlist.id), quote=True)}">
    <button class="danger" type="submit">Stop managing (keep Spotify playlist)</button>
  </form>
</article>
"""


def _template_card(
    template: PlaylistTemplate,
    *,
    catalog: BuiltInCatalog,
    csrf_token: str,
    spotify_connected: bool,
) -> str:
    source_controls = _source_checkboxes(
        catalog,
        selected=set(template.default_source_ids),
        prefix=f"new-{template.id}",
    )
    disabled = "" if spotify_connected else " disabled"
    hint = "" if spotify_connected else '<p class="muted">Connect Spotify before activation.</p>'
    duration_minutes, duration_remainder = divmod(template.duration_policy.default_max_seconds, 60)
    description = html.escape(template.description)
    return f"""
<article class="card playlist-card">
  <div class="card-head">
    {_cover_image(template.cover_id, template.display_name)}
    <div>
      <h3>{html.escape(template.display_name)}</h3>
      <p>{status_badge("Built-in template", tone="muted")}</p>
      <p class="muted">Creates a Spotify playlist and uploads the bundled cover when permitted.</p>
    </div>
  </div>
  <form method="post" action="/admin/playlists/activate">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <input type="hidden" name="template_id" value="{html.escape(str(template.id), quote=True)}">
    <input type="hidden" name="cover_id" value="{html.escape(template.cover_id, quote=True)}">
    <label>Name
      <input type="text" name="display_name" required maxlength="{MAX_PLAYLIST_NAME_LENGTH}"
             value="{html.escape(template.display_name, quote=True)}">
    </label>
    <label>Description
      <textarea name="description"
                maxlength="{MAX_PLAYLIST_DESCRIPTION_LENGTH}">{description}</textarea>
    </label>
    <div class="duration-row">
      <label>Maximum duration · minutes
        <input type="number" name="max_duration_minutes" required min="0" step="1"
               value="{duration_minutes}">
      </label>
      <label>Seconds
        <input type="number" name="max_duration_seconds_remainder" required min="0" max="59"
               step="1" value="{duration_remainder}">
      </label>
    </div>
    <fieldset><legend>Sources</legend>{source_controls}</fieldset>
    {hint}
    <button type="submit"{disabled}>Create Spotify playlist</button>
  </form>
</article>
"""


def _source_checkboxes(
    catalog: BuiltInCatalog,
    *,
    selected: set[SourceId],
    prefix: str,
) -> str:
    rows = []
    for source in catalog.sources:
        checked = " checked" if source.id in selected else ""
        control_id = f"{prefix}-{source.id}"
        safe_control_id = html.escape(control_id, quote=True)
        rows.append(
            f'<label for="{safe_control_id}">'
            f'<input id="{safe_control_id}" type="checkbox" name="source_id" '
            f'value="{html.escape(str(source.id), quote=True)}"{checked}> '
            f"{html.escape(_source_label(source))}</label>"
        )
    return "".join(rows)


def _provisioning_recovery(intent: object, *, csrf_token: str) -> str:
    if intent is None or getattr(intent, "state", None) is not ProvisioningState.REQUEST_STARTED:
        return ""
    return f"""
<section class="warning">
  <h2>Playlist creation needs recovery</h2>
  <p>A Spotify playlist creation may have succeeded before this installation saved its
     destination. Do not create another playlist until this is resolved.</p>
  <form method="post" action="/admin/provisioning/adopt">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <label>Existing Spotify playlist ID
      <input type="text" name="destination_id" required minlength="22" maxlength="22">
    </label>
    <p class="muted">The playlist must exist and be owned by the authorized Spotify user.</p>
    <button type="submit">Adopt existing playlist</button>
  </form>
  <form method="post" action="/admin/provisioning/clear">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <p class="muted">Clear only after verifying that no usable Spotify playlist needs adoption.
       This does not change Spotify.</p>
    <button class="danger" type="submit">Clear uncertain activation</button>
  </form>
</section>
"""


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
        outcome = source_outcomes.get(source.id)
        if outcome is None:
            health = status_badge("No cycle data", tone="muted")
        elif outcome.ok:
            health = status_badge("Healthy", tone="success")
        else:
            health = status_badge("Failed", tone="danger")
        playlists = ", ".join(used_by.get(source.id, [])) or "Not used"
        rows.append(
            "<tr>"
            f"<td>{html.escape(source.display_name)}</td>"
            f"<td>{html.escape(', '.join(str(value) for value in source.countries))}</td>"
            f"<td><code>{html.escape(source.editorial_scope.value)}</code></td>"
            f"<td>{html.escape(', '.join(str(value) for value in source.languages))}</td>"
            f"<td>{health}</td>"
            f"<td>{html.escape(playlists)}</td>"
            f"<td>{status_badge('Built-in', tone='muted')}</td>"
            "</tr>"
        )
    return (
        '<div class="card table-card"><div class="table-scroll">'
        "<table><thead><tr>"
        '<th scope="col">Source</th><th scope="col">Country</th>'
        '<th scope="col">Scope</th><th scope="col">Language</th>'
        '<th scope="col">Health</th><th scope="col">Used by</th>'
        '<th scope="col">Ownership</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div></div>"
    )


def _source_label(source: SourceDefinition) -> str:
    origin = ",".join(str(value) for value in source.countries)
    language = ",".join(str(value) for value in source.languages)
    return f"{source.display_name} ({origin} · {source.editorial_scope.value} · {language})"


def _cover_image(cover_id: str, label: str) -> str:
    safe_id = urllib.parse.quote(cover_id, safe="")
    return (
        f'<img class="cover" src="/admin/covers/{safe_id}.jpg" '
        f'alt="Cover for {html.escape(label, quote=True)}">'
    )


def _message(kind: str, value: str | None) -> str:
    return "" if value is None else f'<p class="{kind}">{html.escape(value)}</p>'


def _lan_warning() -> str:
    return (
        '<div class="warning"><strong>LAN development mode.</strong> '
        "Administration still uses HTTP Basic authentication without TLS in this step. Keep port "
        "8788 only on a trusted private network and never expose it to the Internet.</div>"
    )
