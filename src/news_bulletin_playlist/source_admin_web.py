"""Server-rendered forms for reviewed Spotify source intake."""

from __future__ import annotations

import html

from news_bulletin_playlist.admin_web import render_admin_shell, render_lan_warning
from news_bulletin_playlist.custom_source_policy import CUSTOM_SOURCE_PARSERS
from news_bulletin_playlist.duration_profile import profile_as_dict
from news_bulletin_playlist.models import EditorialScope, SourceOriginKind
from news_bulletin_playlist.source_inspection import SourceInspection

_PREVIEW_LIMIT = 30


def render_new_source_page(*, csrf_token: str, lan_mode: bool) -> bytes:
    body = (
        (render_lan_warning() if lan_mode else "")
        + f"""
<section class="section admin-section">
  <h2>Add Spotify source</h2>
  <p>Enter a Spotify show ID, URI or canonical URL. Inspection does not save a source.</p>
  <form method="post" action="/admin/sources/inspect">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
    <label for="locator">Spotify show ID, URI or URL</label>
    <input id="locator" name="locator" type="text" maxlength="512" required>
    <button class="button button-primary" type="submit">Inspect source</button>
  </form>
</section>
"""
    )
    return render_admin_shell(
        title="Add source",
        active_tab="sources",
        intro="Inspect a Spotify show before creating it.",
        body_html=body,
    )


def render_source_review_page(
    report: SourceInspection, *, csrf_token: str, lan_mode: bool
) -> bytes:
    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    rows = "".join(
        "<tr>"
        f"<td>{item.catalogue_position}</td><td>{esc(item.title)}</td>"
        f"<td>{esc(item.release_date)} ({esc(item.release_date_precision)})</td>"
        f"<td>{'Unknown' if item.duration_seconds is None else item.duration_seconds}</td>"
        f"<td>{esc(item.collection_filter_outcome)}</td>"
        f"<td>{esc(item.parser_outcome)}</td>"
        f"<td>{esc(item.edition_at) if item.edition_at else 'Unset'}</td>"
        f"<td>{esc(item.timestamp_source.value) if item.timestamp_source else 'Unset'}</td>"
        f"<td>{esc(item.ordering_evidence)}</td>"
        f"<td>{esc(', '.join(item.warnings) or 'None')}</td></tr>"
        for item in report.episodes[:_PREVIEW_LIMIT]
    )
    buckets = "".join(
        f"<li>{esc(name)}: {count}</li>"
        for name, count in profile_as_dict(report.duration_buckets).items()
    )
    observed_buckets = "".join(
        f"<li>{esc(name)}: {count}</li>"
        for name, count in profile_as_dict(report.observed_duration_buckets).items()
    )
    warnings = "".join(f"<li>{esc(value)}</li>" for value in report.warnings)
    scopes = "".join(
        f'<option value="{scope.value}">{scope.value}</option>' for scope in EditorialScope
    )
    origins = "".join(
        f'<option value="{kind.value}">{kind.value.title()}</option>' for kind in SourceOriginKind
    )
    parsers = "".join(
        f'<option value="{esc(parser)}"'
        f"{(' selected' if parser == str(report.parser_id) else '')}"
        f">{esc(parser)}</option>"
        for parser in sorted(CUSTOM_SOURCE_PARSERS)
    )
    body = (
        (render_lan_warning() if lan_mode else "")
        + f"""
<section class="section admin-section">
  <h2>Provider-derived evidence</h2>
  <p>Spotify show: <strong>{esc(report.display_name_hint)}</strong></p>
  <p>Publisher hint: {esc(report.publisher_hint or "Unavailable")}</p>
  <p>Language hints: {esc(", ".join(report.language_hints) or "Unavailable")}</p>
  <p>Canonical ID: <code>{esc(report.spotify_show_id)}</code></p>
  <p>URI: <code>{esc(report.spotify_uri)}</code></p>
  <p><a href="{esc(report.spotify_url)}" target="_blank" rel="noopener noreferrer">
     Open canonical Spotify show</a></p>
  <p>Catalogue coverage proven: {str(report.coverage_proven).lower()};
     observed: {report.observed_episodes}; accepted: {report.accepted_episodes};
     filter-rejected: {report.filter_rejected_count};
     parser fallback: {report.parser_fallback_count}.</p>
  <p>Duration known: {report.duration_known_count}; missing: {report.duration_missing_count};
     min: {esc(report.min_duration_seconds)}; max: {esc(report.max_duration_seconds)} seconds.</p>
  <h3>Accepted duration profile</h3><ul>{buckets}</ul>
  <h3>Observed duration profile</h3><ul>{observed_buckets}</ul>
  <p>Observed items without duration: {report.observed_duration_missing_count}.</p>
  <p>Warnings are evidence, not an ordering guarantee.</p><ul>{warnings}</ul>
  <p><a href="/admin/sources/inspection-report.json">Download inspection report</a></p>
  <p>Showing {min(len(report.episodes), _PREVIEW_LIMIT)} of
     {len(report.episodes)} evidence rows.</p>
  <div class="table-scroll"><table><thead><tr><th>Position</th><th>Title</th><th>Release</th>
    <th>Seconds</th><th>Filter</th><th>Parser</th><th>Edition</th>
    <th>Provenance</th><th>Ordering</th><th>Warnings</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
</section>
<section class="section admin-section">
  <h2>Operator classification</h2>
  <p>Confirm these fields explicitly. Spotify hints do not set origin, scope or timezone.</p>
  <form method="post" action="/admin/sources/create">
    <input type="hidden" name="csrf_token" value="{esc(csrf_token)}">
    <input type="hidden" name="spotify_show_id" value="{esc(report.spotify_show_id)}">
    <label>Display name <input name="display_name" maxlength="120" required
           value="{esc(report.display_name_hint)}"></label>
    <label>Origin kind <select name="origin_kind">{origins}</select></label>
    <label>Origin code <input name="origin_code" maxlength="12" required></label>
    <label>Language <input name="language" required></label>
    <label>Editorial scope <select name="editorial_scope">{scopes}</select></label>
    <label>IANA timezone <input name="timezone" required></label>
    <label>Parser <select name="parser_id">{parsers}</select></label>
    <input type="hidden" name="collection_filter_id"
           value="{esc(report.collection_filter_id or "")}">
    <button class="button button-primary" type="submit">Validate again and add source</button>
  </form>
</section>
"""
    )
    return render_admin_shell(
        title="Review source",
        active_tab="sources",
        intro="Review bounded evidence and classify this source.",
        body_html=body,
    )
