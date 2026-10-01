"""Managed-playlist admin rendering and form validation helpers."""

from __future__ import annotations

import html
import urllib.parse
from collections.abc import Mapping

from news_bulletin_playlist.admin_web import render_admin_shell, render_lan_warning
from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.engine import EngineCycleResult
from news_bulletin_playlist.managed_admin import (
    MAX_PLAYLIST_DESCRIPTION_LENGTH,
    MAX_PLAYLIST_NAME_LENGTH,
    ManagedAdminSnapshot,
)
from news_bulletin_playlist.managed_duration import validate_persisted_duration_seconds
from news_bulletin_playlist.managed_provisioning import ProvisioningIntent, ProvisioningState
from news_bulletin_playlist.managed_state import (
    ManagedPlaylist,
    ManagedSource,
    ManagedState,
    effective_sources,
)
from news_bulletin_playlist.models import PlaylistId, SourceDefinition, SourceId
from news_bulletin_playlist.spotify.auth import AuthorizationState
from news_bulletin_playlist.spotify.authorization_display import authorization_state_label
from news_bulletin_playlist.web_ui import status_badge


def render_managed_admin_page(
    *,
    snapshot: ManagedAdminSnapshot,
    catalog: BuiltInCatalog,
    spotify_state: AuthorizationState | None,
    csrf_token: str,
    last_cycle: EngineCycleResult | None,
    lan_mode: bool,
    state: ManagedState,
    notice: str | None = None,
    error: str | None = None,
) -> bytes:
    """Render playlist policy and lifecycle controls in the shared admin shell."""

    spotify_connected = spotify_state is AuthorizationState.CONNECTED
    sources = effective_sources(catalog, state)
    spotify_label = authorization_state_label(spotify_state)
    managed = (
        "".join(
            _managed_card(
                playlist,
                catalog=catalog,
                csrf_token=csrf_token,
                last_cycle=last_cycle,
                custom_sources=state.sources,
                sources=sources,
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
                    csrf_token=csrf_token,
                    spotify_connected=spotify_connected,
                    sources=sources,
                    custom_source_ids=frozenset(source.id for source in state.sources),
                )
                for template in snapshot.available_templates
            )
            or '<p class="empty">No additional built-in playlists are available.</p>'
        )
    spotify_action = "Reconnect Spotify" if spotify_connected else "Connect Spotify"
    actions = f"""
<form method="post" action="/admin/spotify/connect">
  {_hidden_input("csrf_token", csrf_token)}
  <button class="button" type="submit">{html.escape(spotify_action)}</button>
</form>
"""
    body = "".join(
        (
            render_lan_warning() if lan_mode else "",
            _message("notice", notice),
            _message("error", error),
            _provisioning_recovery(snapshot.provisioning_intent, csrf_token=csrf_token),
            f"""
<section class="section admin-section" aria-labelledby="active-playlists-heading">
  <div class="section-head">
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
<section class="section admin-section" aria-labelledby="available-playlists-heading">
  <div class="section-head">
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


def single_form_value(
    form: Mapping[str, list[str]],
    name: str,
    *,
    required: bool = True,
) -> str:
    values = form.get(name, [])
    if not values and not required:
        return ""
    if len(values) != 1:
        raise ValueError(f"Exactly one {name} value is required")
    return values[0]


def max_duration_seconds_from_form(form: Mapping[str, list[str]]) -> int | None:
    values = form.get("max_duration_minutes", [])
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("Exactly one max_duration_minutes value is required")
    raw = values[0].strip()
    remainder_values = form.get("max_duration_seconds_remainder", [])
    if not remainder_values:
        remainder = 0  # Compatibility with existing minute-only POSTs.
    elif len(remainder_values) == 1:
        try:
            remainder = int(remainder_values[0].strip())
        except ValueError as exc:
            raise ValueError("max_duration_seconds_remainder must be an integer") from exc
    else:
        raise ValueError("Exactly one max_duration_seconds_remainder value is required")
    try:
        minutes = int(raw)
    except ValueError as exc:
        raise ValueError("max_duration_minutes must be an integer") from exc
    if minutes < 0 or not 0 <= remainder < 60:
        raise ValueError("maximum duration must be positive with seconds remainder from 0 to 59")
    try:
        return validate_persisted_duration_seconds(minutes * 60 + remainder)
    except ValueError as exc:
        raise ValueError("maximum duration must be positive") from exc


def playlist_id_from_form(form: Mapping[str, list[str]]) -> PlaylistId:
    value = single_form_value(form, "playlist_id").strip()
    if not value:
        raise ValueError("playlist_id must not be empty")
    return PlaylistId(value)


def _managed_card(
    playlist: ManagedPlaylist,
    *,
    catalog: BuiltInCatalog,
    csrf_token: str,
    last_cycle: EngineCycleResult | None,
    custom_sources: tuple[ManagedSource, ...],
    sources: tuple[SourceDefinition, ...],
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
    spotify_url = "https://open.spotify.com/playlist/" + urllib.parse.quote(
        playlist.destination.external_id,
        safe="",
    )
    template = catalog.playlist(playlist.template_id)
    effective_duration_seconds = (
        template.duration_policy.default_max_seconds
        if playlist.max_duration_seconds is None
        else playlist.max_duration_seconds
    )
    state = (
        status_badge("Active", tone="success")
        if playlist.enabled
        else status_badge("Paused", tone="warning")
    )
    fields = _playlist_policy_fields(
        display_name=playlist.display_name,
        description=playlist.description,
        duration_seconds=effective_duration_seconds,
        sources=sources,
        custom_source_ids=frozenset(source.id for source in custom_sources),
        selected_source_ids=set(playlist.source_ids),
        source_prefix=f"edit-{playlist.id}",
        enabled=playlist.enabled,
    )
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
    {_hidden_input("csrf_token", csrf_token)}
    {_hidden_input("playlist_id", str(playlist.id))}
    {_hidden_input("cover_id", playlist.cover_id)}
    {fields}
    <button class="button" type="submit">Save playlist</button>
  </form>
  <form method="post" action="/admin/playlists/sync">
    {_hidden_input("csrf_token", csrf_token)}
    {_hidden_input("playlist_id", str(playlist.id))}
    <button class="button" type="submit">Apply Spotify metadata &amp; cover</button>
  </form>
  <form method="post" action="/admin/playlists/stop">
    {_hidden_input("csrf_token", csrf_token)}
    {_hidden_input("playlist_id", str(playlist.id))}
    <button class="button button-danger" type="submit">
      Stop managing (keep Spotify playlist)
    </button>
  </form>
</article>
"""


def _template_card(
    template: PlaylistTemplate,
    *,
    catalog: BuiltInCatalog,
    csrf_token: str,
    spotify_connected: bool,
    sources: tuple[SourceDefinition, ...],
    custom_source_ids: frozenset[SourceId],
) -> str:
    disabled = "" if spotify_connected else " disabled"
    hint = "" if spotify_connected else '<p class="muted">Connect Spotify before activation.</p>'
    fields = _playlist_policy_fields(
        display_name=template.display_name,
        description=template.description,
        duration_seconds=template.duration_policy.default_max_seconds,
        sources=sources,
        custom_source_ids=custom_source_ids,
        selected_source_ids=set(template.default_source_ids),
        source_prefix=f"new-{template.id}",
        enabled=None,
    )
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
    {_hidden_input("csrf_token", csrf_token)}
    {_hidden_input("template_id", str(template.id))}
    {_hidden_input("cover_id", template.cover_id)}
    {fields}
    {hint}
    <button class="button" type="submit"{disabled}>Create Spotify playlist</button>
  </form>
</article>
"""


def _playlist_policy_fields(
    *,
    display_name: str,
    description: str,
    duration_seconds: int,
    sources: tuple[SourceDefinition, ...],
    custom_source_ids: frozenset[SourceId],
    selected_source_ids: set[SourceId],
    source_prefix: str,
    enabled: bool | None,
) -> str:
    """Render the policy fields shared by existing playlists and activation templates."""

    duration_minutes, duration_remainder = divmod(duration_seconds, 60)
    source_controls = _source_checkboxes(
        tuple(source for source in sources if source.enabled),
        custom_source_ids=custom_source_ids,
        selected=selected_source_ids,
        prefix=source_prefix,
    )
    inactive_sources = tuple(
        source for source in sources if not source.enabled and source.id in selected_source_ids
    )
    inactive_controls = ""
    if inactive_sources:
        inactive_controls = (
            "<fieldset><legend>Inactive assignments</legend>"
            '<p class="muted">Disabled sources contribute no episodes. They return automatically '
            "when re-enabled. Keep checked to retain the assignment; uncheck to permanently "
            "unassign.</p>"
            + _source_checkboxes(
                inactive_sources,
                custom_source_ids=custom_source_ids,
                selected=selected_source_ids,
                prefix=source_prefix,
            )
            + "</fieldset>"
        )
    enabled_control = ""
    if enabled is not None:
        checked = " checked" if enabled else ""
        enabled_control = (
            f'<label><input type="checkbox" name="enabled" value="1"{checked}> Active</label>'
        )
    return f"""
    <label>Name
      <input type="text" name="display_name" required maxlength="{MAX_PLAYLIST_NAME_LENGTH}"
             value="{html.escape(display_name, quote=True)}">
    </label>
    <label>Description
      <textarea name="description"
                maxlength="{MAX_PLAYLIST_DESCRIPTION_LENGTH}">{html.escape(description)}</textarea>
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
    {inactive_controls}
    {enabled_control}
"""


def _source_checkboxes(
    sources: tuple[SourceDefinition, ...],
    *,
    custom_source_ids: frozenset[SourceId],
    selected: set[SourceId],
    prefix: str,
) -> str:
    rows = []
    for source in sources:
        checked = " checked" if source.id in selected else ""
        control_id = f"{prefix}-{source.id}"
        safe_control_id = html.escape(control_id, quote=True)
        rows.append(
            f'<label for="{safe_control_id}">'
            f'<input id="{safe_control_id}" type="checkbox" name="source_id" '
            f'value="{html.escape(str(source.id), quote=True)}"{checked}> '
            f"{html.escape(_source_label(source))}"
            f"{' · Custom' if source.id in custom_source_ids else ''}"
            f"{' — Disabled' if not source.enabled else ''}"
            "</label>"
        )
    return "".join(rows)


def _provisioning_recovery(
    intent: ProvisioningIntent | None,
    *,
    csrf_token: str,
) -> str:
    if intent is None or intent.state is not ProvisioningState.REQUEST_STARTED:
        return ""
    return f"""
<section class="warning" aria-labelledby="provisioning-recovery-heading">
  <h2 id="provisioning-recovery-heading">Playlist creation needs recovery</h2>
  <p>A Spotify playlist creation may have succeeded before this installation saved its
     destination. Do not create another playlist until this is resolved.</p>
  <form method="post" action="/admin/provisioning/adopt">
    {_hidden_input("csrf_token", csrf_token)}
    <label>Existing Spotify playlist ID
      <input type="text" name="destination_id" required minlength="22" maxlength="22">
    </label>
    <p class="muted">The playlist must exist and be owned by the authorized Spotify user.</p>
    <button class="button" type="submit">Adopt existing playlist</button>
  </form>
  <form method="post" action="/admin/provisioning/clear">
    {_hidden_input("csrf_token", csrf_token)}
    <p class="muted">Clear only after verifying that no usable Spotify playlist needs adoption.
       This does not change Spotify.</p>
    <button class="button button-danger" type="submit">Clear uncertain activation</button>
  </form>
</section>
"""


def _source_label(source: SourceDefinition) -> str:
    language = ",".join(str(value) for value in source.languages)
    return f"{source.display_name} ({source.origin} · {source.editorial_scope.value} · {language})"


def _cover_image(cover_id: str, label: str) -> str:
    safe_id = urllib.parse.quote(cover_id, safe="")
    return (
        f'<img class="cover" src="/admin/covers/{safe_id}.jpg" '
        f'alt="Cover for {html.escape(label, quote=True)}">'
    )


def _hidden_input(name: str, value: str) -> str:
    return (
        f'<input type="hidden" name="{html.escape(name, quote=True)}" '
        f'value="{html.escape(value, quote=True)}">'
    )


def _message(kind: str, value: str | None) -> str:
    return "" if value is None else f'<p class="{kind}">{html.escape(value)}</p>'
