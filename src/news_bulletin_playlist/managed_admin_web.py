"""Pure HTML rendering helpers for managed playlist administration."""

from __future__ import annotations

import html
import urllib.parse
from collections.abc import Mapping

from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.engine import EngineCycleResult
from news_bulletin_playlist.managed_admin import (
    MAX_PLAYLIST_DESCRIPTION_LENGTH,
    MAX_PLAYLIST_NAME_LENGTH,
    ManagedAdminSnapshot,
)
from news_bulletin_playlist.managed_state import ManagedPlaylist
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.spotify.auth import AuthorizationState


def render_managed_admin_page(
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
    """Render one authenticated dashboard for playlists, templates and sources."""
    spotify_connected = spotify_state is AuthorizationState.CONNECTED
    spotify_label = _spotify_label(spotify_state)
    warning = _lan_warning() if lan_mode else ""
    notice_html = _message("notice", notice)
    error_html = _message("error", error)
    csrf = html.escape(csrf_token, quote=True)
    managed = "".join(
        _managed_card(
            playlist,
            catalog=catalog,
            csrf_token=csrf,
            last_cycle=last_cycle,
        )
        for playlist in snapshot.managed
    ) or '<p class="empty">No managed playlists yet.</p>'
    available = "".join(
        _template_card(
            template,
            catalog=catalog,
            csrf_token=csrf,
            spotify_connected=spotify_connected,
        )
        for template in snapshot.available_templates
    ) or '<p class="empty">No additional built-in playlists are available.</p>'
    sources = _sources_table(snapshot, catalog, last_cycle)
    spotify_action = "Reconnect Spotify" if spotify_connected else "Connect Spotify"

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Administration · News Bulletin Playlists</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: system-ui, sans-serif; max-width: 78rem; margin: 2rem auto;
           padding: 0 1rem 4rem; line-height: 1.45; }}
    header {{ display: flex; gap: 1rem; justify-content: space-between; align-items: start;
              flex-wrap: wrap; }}
    section {{ margin-top: 2.25rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(19rem, 1fr));
             gap: 1rem; }}
    .card {{ border: 1px solid #8888; border-radius: .75rem; padding: 1rem; }}
    .card-head {{ display: flex; gap: .9rem; align-items: start; }}
    .cover {{ width: 5rem; height: 5rem; object-fit: cover; border-radius: .45rem;
              border: 1px solid #8888; flex: 0 0 auto; }}
    label {{ display: block; font-weight: 650; margin-top: .65rem; }}
    input[type=text], textarea {{ box-sizing: border-box; width: 100%; font: inherit;
                                 padding: .45rem; }}
    textarea {{ min-height: 5.5rem; resize: vertical; }}
    fieldset {{ margin: .8rem 0; border: 1px solid #8888; }}
    fieldset label {{ font-weight: 400; margin: .25rem 0; }}
    button {{ font: inherit; padding: .48rem .75rem; margin-top: .45rem; }}
    .danger {{ border-color: #b42318; }}
    .warning {{ border: 2px solid #9a6700; padding: .8rem; border-radius: .5rem; }}
    .notice {{ border-left: .35rem solid #1a7f37; padding: .6rem .8rem; }}
    .error {{ border-left: .35rem solid #b42318; padding: .6rem .8rem; }}
    .muted, .empty {{ opacity: .75; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; vertical-align: top; border-bottom: 1px solid #8885;
              padding: .5rem .4rem; }}
    code {{ font-family: ui-monospace, monospace; }}
    .inline {{ display: inline; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Administration</h1>
      <p>Spotify authorization: <strong>{html.escape(spotify_label)}</strong></p>
    </div>
    <form method="post" action="/admin/spotify/connect">
      <input type="hidden" name="csrf_token" value="{csrf}">
      <button type="submit">{html.escape(spotify_action)}</button>
    </form>
  </header>
  {warning}
  {notice_html}
  {error_html}

  <section>
    <h2>Active playlists</h2>
    <p class="muted">Each playlist keeps its own source selection. A source shared by several
       active playlists is still fetched only once per engine cycle.</p>
    <div class="grid">{managed}</div>
  </section>

  <section>
    <h2>Available playlists</h2>
    <p class="muted">Review the built-in defaults before creating a private Spotify playlist.</p>
    <div class="grid">{available}</div>
  </section>

  <section>
    <h2>Sources</h2>
    {sources}
  </section>

  <p><a href="/">Return to read-only status</a></p>
</body>
</html>
"""
    return document.encode("utf-8")


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
    result = "No cycle data"
    if outcome is not None:
        result = "Success" if outcome.ok else f"Failed: {outcome.error or 'unknown error'}"
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
    description_control = (
        f'<textarea name="description" maxlength="{MAX_PLAYLIST_DESCRIPTION_LENGTH}">'
        f"{html.escape(playlist.description)}</textarea>"
    )
    return f"""
<article class="card">
  <div class="card-head">
    {_cover_image(playlist.cover_id, playlist.display_name)}
    <div>
      <h3>{html.escape(playlist.display_name)}</h3>
      <p><strong>{'Active' if playlist.enabled else 'Paused'}</strong> · Private when created</p>
      <p><a href="{html.escape(spotify_url, quote=True)}" target="_blank"
            rel="noopener noreferrer">Open in Spotify</a></p>
      <p class="muted">Last result: {html.escape(result)}</p>
      <p class="muted">Reconnect Spotify once for image permission,
        then apply Spotify metadata and cover.</p>
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
      {description_control}
    </label>
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
    description_control = (
        f'<textarea name="description" maxlength="{MAX_PLAYLIST_DESCRIPTION_LENGTH}">'
        f"{html.escape(template.description)}</textarea>"
    )
    return f"""
<article class="card">
  <div class="card-head">
    {_cover_image(template.cover_id, template.display_name)}
    <div>
      <h3>{html.escape(template.display_name)}</h3>
      <p class="muted">Built-in template · creates a private Spotify playlist</p>
      <p class="muted">The bundled cover is uploaded when Spotify grants image permission.</p>
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
      {description_control}
    </label>
    <fieldset><legend>Sources</legend>{source_controls}</fieldset>
    {hint}
    <button type="submit"{disabled}>Create private playlist</button>
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
        rows.append(
            f'<label for="{html.escape(control_id, quote=True)}">'
            f'<input id="{html.escape(control_id, quote=True)}" type="checkbox" '
            f'name="source_id" value="{html.escape(str(source.id), quote=True)}"{checked}> '
            f'{html.escape(source.display_name)}</label>'
        )
    return "".join(rows)


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
        health = "No cycle data" if outcome is None else ("Healthy" if outcome.ok else "Failed")
        playlists = ", ".join(used_by.get(source.id, [])) or "Not used"
        rows.append(
            "<tr>"
            f"<td>{html.escape(source.display_name)}</td>"
            f"<td>{html.escape(', '.join(str(value) for value in source.countries))}</td>"
            f"<td>{html.escape(', '.join(str(value) for value in source.languages))}</td>"
            f"<td>{html.escape(health)}</td>"
            f"<td>{html.escape(playlists)}</td>"
            "<td>Built-in</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Source</th><th>Country</th><th>Language</th>"
        "<th>Health</th><th>Used by</th><th>Origin</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _cover_image(cover_id: str, label: str) -> str:
    safe_id = urllib.parse.quote(cover_id, safe="")
    return (
        f'<img class="cover" src="/admin/covers/{safe_id}.jpg" '
        f'alt="Cover for {html.escape(label, quote=True)}">'
    )


def _message(kind: str, value: str | None) -> str:
    if value is None:
        return ""
    return f'<p class="{kind}">{html.escape(value)}</p>'


def _lan_warning() -> str:
    return (
        '<div class="warning"><strong>LAN development mode.</strong> '
        "Administration uses HTTP Basic authentication without TLS. Keep port 8788 only on a "
        "trusted private network and never expose it to the Internet.</div>"
    )


def _spotify_label(state: AuthorizationState | None) -> str:
    if state is None:
        return "Not configured"
    return {
        AuthorizationState.DISCONNECTED: "Not connected",
        AuthorizationState.CONNECTED: "Connected",
        AuthorizationState.REAUTH_REQUIRED: "Reauthorization required",
        AuthorizationState.ERROR: "Authorization state error",
    }[state]


def single_form_value(
    form: Mapping[str, list[str]],
    name: str,
    *,
    required: bool = True,
) -> str:
    """Return exactly one form value and reject duplicates or missing required fields."""
    values = form.get(name, [])
    if not values and not required:
        return ""
    if len(values) != 1:
        raise ValueError(f"Exactly one {name} value is required")
    return values[0]


def playlist_id_from_form(form: Mapping[str, list[str]]) -> PlaylistId:
    """Parse one managed playlist ID from a form."""
    value = single_form_value(form, "playlist_id").strip()
    if not value:
        raise ValueError("playlist_id must not be empty")
    return PlaylistId(value)
