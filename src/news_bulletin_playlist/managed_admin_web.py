"""Managed-playlist admin rendering and form validation helpers."""

from __future__ import annotations

import html
import urllib.parse
from collections.abc import Mapping

from news_bulletin_playlist.admin_web import render_admin_shell, render_lan_warning
from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.engine import EngineCycleResult
from news_bulletin_playlist.managed_admin import (
    MAX_PLAYLIST_DESCRIPTION_LENGTH,
    MAX_PLAYLIST_NAME_LENGTH,
    MAX_SPOTIFY_BASE_DESCRIPTION_LENGTH,
    ManagedAdminSnapshot,
)
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
    custom_sources = frozenset(source.id for source in state.sources)
    drafts = (
        "".join(
            _custom_draft_card(
                draft,
                catalog=catalog,
                csrf_token=csrf_token,
                sources=sources,
                custom_source_ids=custom_sources,
                managed=snapshot.managed,
                provisioning_intent=snapshot.provisioning_intent,
            )
            for draft in snapshot.custom_templates
        )
        or '<p class="empty">No custom playlist drafts yet.</p>'
    )
    new_draft = _custom_draft_create_form(
        catalog=catalog,
        csrf_token=csrf_token,
        sources=sources,
        custom_source_ids=custom_sources,
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
  <p class="muted">Each playlist keeps its own source selection and duration range. A source
     shared by several playlists is still fetched only once per engine cycle.</p>
  <div class="admin-grid">{managed}</div>
</section>
<section class="section admin-section" aria-labelledby="custom-playlists-heading">
  <div class="section-head">
    <div>
      <p class="eyebrow">Local drafts</p>
      <h2 id="custom-playlists-heading">My playlists</h2>
    </div>
  </div>
  <p class="muted">Save and edit your own playlist templates here. Draft changes stay under
     /data and do not call Spotify. Publishing a draft will be a separate action.</p>
  {new_draft}
  <div class="admin-grid">{drafts}</div>
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


def duration_minutes_from_form(form: Mapping[str, list[str]], bound: str) -> int | None:
    name = f"{bound}_duration_minutes"
    values = form.get(name, [])
    if not values:
        return None
    raw = single_form_value(form, name)
    try:
        minutes = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if minutes < 0:
        raise ValueError("duration minutes must be non-negative")
    return minutes


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
    state = (
        status_badge("Active", tone="success")
        if playlist.enabled
        else status_badge("Paused", tone="warning")
    )
    fields = _playlist_policy_fields(
        display_name=playlist.display_name,
        description=playlist.description,
        min_duration_seconds=playlist.min_duration_seconds,
        duration_seconds=playlist.max_duration_seconds,
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
        min_duration_seconds=template.duration_policy.default_min_seconds,
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


def _custom_draft_fields(
    draft: LocalPlaylistTemplate | None,
    *,
    catalog: BuiltInCatalog,
    sources: tuple[SourceDefinition, ...],
    custom_source_ids: frozenset[SourceId],
    prefix: str,
) -> str:
    if draft is None:
        name, description, low, high, selected = "", "", 0, 1800, set()
    else:
        name, description = draft.display_name, draft.description
        low, high = draft.min_duration_seconds, draft.max_duration_seconds
        selected = set(draft.default_source_ids)
    policy = _playlist_policy_fields(
        display_name=name,
        description=description,
        min_duration_seconds=low,
        duration_seconds=high,
        sources=sources,
        custom_source_ids=custom_source_ids,
        selected_source_ids=selected,
        source_prefix=prefix,
        enabled=None,
    )
    covers = "".join(
        f'<option value="{html.escape(template.cover_id, quote=True)}"'
        f'{" selected" if template.cover_id == (draft.cover_id if draft else "international_spanish_news") else ""}>'
        f"{html.escape(template.display_name)}</option>"
        for template in catalog.playlists
    )
    retention = 48 if draft is None else draft.retention_hours
    maximum = 100 if draft is None else draft.max_episodes
    languages = "" if draft is None else ", ".join(str(x) for x in draft.languages)
    countries = "" if draft is None else ", ".join(str(x) for x in draft.countries)
    hidden = ""
    if draft is not None:
        checked = " checked" if draft.hidden else ""
        hidden = (
            f'<label><input type="checkbox" name="hidden" value="1"{checked}>'
            " Hide draft from future activation choices</label>"
        )
    return f"""
{policy}
<label>Bundled cover (custom uploads are not yet supported)
  <select name="cover_id">{covers}</select>
</label>
<div class="duration-row">
  <label>Retention · hours
    <input type="number" name="retention_hours" min="1" max="720" required
           value="{retention}">
  </label>
  <label>Maximum episodes
    <input type="number" name="max_episodes" min="1" max="100" required
           value="{maximum}">
  </label>
</div>
<label>Languages · comma-separated tags (optional)
  <input type="text" name="language_tags" placeholder="es-ES, en"
         value="{html.escape(languages, quote=True)}">
</label>
<label>Countries · comma-separated ISO codes (optional)
  <input type="text" name="country_codes" placeholder="ES, FR"
         value="{html.escape(countries, quote=True)}">
</label>
{hidden}
<p class="muted">Local draft only. No Spotify destination is created, changed or removed.</p>
"""


def _custom_draft_create_form(
    *,
    catalog: BuiltInCatalog,
    csrf_token: str,
    sources: tuple[SourceDefinition, ...],
    custom_source_ids: frozenset[SourceId],
) -> str:
    fields = _custom_draft_fields(
        None,
        catalog=catalog,
        sources=sources,
        custom_source_ids=custom_source_ids,
        prefix="custom-draft-new",
    )
    return f"""
<details class="card playlist-card">
  <summary>Create new playlist draft</summary>
  <form method="post" action="/admin/playlists/templates/create">
    {_hidden_input("csrf_token", csrf_token)}
    {fields}
    <button class="button button-primary" type="submit">Save local draft</button>
  </form>
</details>
"""


def _custom_draft_card(
    draft: LocalPlaylistTemplate,
    *,
    catalog: BuiltInCatalog,
    csrf_token: str,
    sources: tuple[SourceDefinition, ...],
    custom_source_ids: frozenset[SourceId],
    managed: tuple[ManagedPlaylist, ...],
    provisioning_intent: ProvisioningIntent | None,
) -> str:
    fields = _custom_draft_fields(
        draft,
        catalog=catalog,
        sources=sources,
        custom_source_ids=custom_source_ids,
        prefix=f"custom-draft-{draft.id}",
    )
    referenced = any(playlist.template_id == draft.id for playlist in managed) or (
        provisioning_intent is not None and provisioning_intent.template_id == draft.id
    )
    delete_control = (
        '<p class="muted">Cannot delete while a managed playlist or pending creation uses '
        "this draft.</p>"
        if referenced
        else f"""
<form method="post" action="/admin/playlists/templates/delete">
  {_hidden_input("csrf_token", csrf_token)}
  {_hidden_input("template_id", str(draft.id))}
  <button class="button button-danger" type="submit">Delete local draft</button>
</form>
"""
    )
    visibility = status_badge("Hidden", tone="warning") if draft.hidden else status_badge(
        "Draft", tone="muted"
    )
    return f"""
<article class="card playlist-card">
  <div class="card-head">
    {_cover_image(draft.cover_id, draft.display_name)}
    <div>
      <h3>{html.escape(draft.display_name)}</h3>
      <p>{visibility}</p>
      <p class="muted">Local ID: <code>{html.escape(str(draft.id))}</code></p>
    </div>
  </div>
  <form method="post" action="/admin/playlists/templates/update">
    {_hidden_input("csrf_token", csrf_token)}
    {_hidden_input("template_id", str(draft.id))}
    {fields}
    <button class="button" type="submit">Save draft changes</button>
  </form>
  {delete_control}
</article>
"""


def _playlist_policy_fields(
    *,
    display_name: str,
    description: str,
    duration_seconds: int,
    min_duration_seconds: int,
    sources: tuple[SourceDefinition, ...],
    custom_source_ids: frozenset[SourceId],
    selected_source_ids: set[SourceId],
    source_prefix: str,
    enabled: bool | None,
) -> str:
    """Render the policy fields shared by existing playlists and activation templates."""

    duration_minutes = duration_seconds // 60
    min_duration_minutes = min_duration_seconds // 60
    precision_note = (
        '<p class="muted">Historical precision in seconds is preserved for each bound '
        "whose displayed minutes are unchanged. "
        "Changing minutes sets an exact whole-minute bound.</p>"
        if duration_seconds % 60 or min_duration_seconds % 60
        else ""
    )
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
        if enabled is False and not selected_source_ids.intersection(
            source.id for source in sources if source.enabled
        ):
            inactive_controls += (
                '<p class="warning">This playlist has no enabled sources and remains paused; '
                "its Spotify destination is preserved. Enable an assigned source or select "
                "another enabled source before activating it.</p>"
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
      <textarea name="description">{html.escape(description)}</textarea>
    </label>
    <p class="muted">The server allows {MAX_PLAYLIST_DESCRIPTION_LENGTH} Unicode characters
      in local descriptions.
      To apply Spotify metadata with project attribution, use at most
      {MAX_SPOTIFY_BASE_DESCRIPTION_LENGTH} characters. Longer descriptions stay saved locally;
      shorten them before syncing metadata. Cover upload is independent.</p>
    <label class="attribution-indicator" for="attribution-{html.escape(source_prefix, quote=True)}">
      <input id="attribution-{html.escape(source_prefix, quote=True)}" type="checkbox"
             checked disabled
             aria-describedby="attribution-note-{html.escape(source_prefix, quote=True)}">
      News Bulletin Playlist attribution enabled
    </label>
    <p class="muted" id="attribution-note-{html.escape(source_prefix, quote=True)}">
      The official app includes project attribution when Spotify metadata is explicitly applied.
      Saving this form does not update Spotify; a failed metadata sync does not mean it was applied.
    </p>
    <div class="duration-row">
      <label>Minimum duration · minutes
        <input type="number" name="min_duration_minutes" required min="0" step="1"
               value="{min_duration_minutes}">
      </label>
      <label>Maximum duration · minutes
        <input type="number" name="max_duration_minutes" required min="0" step="1"
               value="{duration_minutes}">
      </label>
    </div>
    {precision_note}
    <p class="muted">Episodes outside this inclusive range are omitted from this playlist only.</p>
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
