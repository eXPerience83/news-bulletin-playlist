"""Small HTML controls for offline portable playlist downloads."""

import html
import urllib.parse

from news_bulletin_playlist.admin_web import render_admin_shell


def portable_controls(kind: str, object_id: str, identity: str | None) -> str:
    query = urllib.parse.urlencode({"kind": kind, "id": object_id})
    path = "export.zip" if identity else "portable/prepare"
    label = "Export portable ZIP" if identity else "Prepare portable export identity"
    return (
        f'<p><a class="button" href="/admin/playlists/{path}?'
        f'{html.escape(query, quote=True)}">{label}</a></p>'
    )


def render_identity_confirmation(
    kind: str, object_id: str, fingerprint: str, schema: int, identity: str | None, csrf: str
) -> bytes:
    def hidden(name: str, value: str) -> str:
        return f'<input type="hidden" name="{name}" value="{html.escape(value, quote=True)}">'

    if identity:
        body = portable_controls(kind, object_id, identity)
    else:
        fields = "".join(
            hidden(k, v)
            for k, v in {
                "kind": kind,
                "id": object_id,
                "fingerprint": fingerprint,
                "csrf_token": csrf,
            }.items()
        )
        acknowledgement = (
            '<label><input type="checkbox" name="ack_v9" value="yes" required>'
            " Adopt managed-state v9. Older images cannot read this state; rollback may require "
            "restoring a compatible complete backup.</label>"
            if schema < 9
            else ""
        )
        body = f"""<p>Prepare a persistent UUIDv4 for {html.escape(kind)}
<code>{html.escape(object_id)}</code>.</p>
<p>This writes local configuration only. It does not change Spotify.
A portable ZIP is not a production backup.</p>
<form method="post" action="/admin/playlists/portable/prepare">
{fields}{acknowledgement}
<label><input type="checkbox" name="confirm" value="yes" required>
Confirm identity preparation</label>
<button class="button button-primary" type="submit">Prepare identity</button></form>"""
    return render_admin_shell(
        title="Portable playlist identity",
        active_tab="playlists",
        intro="Identity preparation is separate from downloading configuration.",
        body_html=body,
    )
