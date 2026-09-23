"""Shared server-rendered administration shell and presentation primitives."""

from __future__ import annotations

import html
from typing import Literal

from news_bulletin_playlist.web_ui import WEB_UI_STYLES

AdminTab = Literal["playlists", "sources", "diagnostics"]

_ADMIN_UI_STYLES = """
.admin-shell {
  width: min(1240px, calc(100% - 2rem));
  min-width: 0;
}

.admin-shell main {
  min-width: 0;
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

.page-actions {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  flex-wrap: wrap;
}

.admin-section {
  min-width: 0;
}

.admin-panel,
.playlist-card {
  min-width: 0;
  padding: 1rem;
}

.admin-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr));
  gap: 0.9rem;
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

.button:disabled {
  cursor: not-allowed;
  opacity: 0.52;
}

.button-danger {
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

.filter-form {
  display: grid;
  grid-template-columns: repeat(5, minmax(8rem, 1fr)) auto;
  gap: 0.7rem;
  align-items: end;
}

.filter-form label {
  margin-top: 0;
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

  .admin-shell .topbar {
    align-items: flex-start;
  }

  .admin-shell .brand span:last-child {
    display: none;
  }

  .page-head {
    padding-top: 2rem;
  }

  .filter-form,
  .duration-row {
    grid-template-columns: 1fr;
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
    """Render one authenticated admin page using project-wide design primitives."""

    tabs: tuple[tuple[AdminTab, str, str], ...] = (
        ("playlists", "Playlists", "/admin/"),
        ("sources", "Sources", "/admin/sources"),
        ("diagnostics", "Diagnostics", "/admin/diagnostics"),
    )
    navigation = "".join(
        _admin_tab(label=label, href=href, active=key == active_tab) for key, label, href in tabs
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
  <div class="shell admin-shell">
    <header>
      <div class="topbar">
        <a class="brand" href="/admin/" aria-label="News Bulletin Playlists administration">
          <span class="brand-mark" aria-hidden="true">NB</span>
          <span>News Bulletin Playlists</span>
        </a>
        <div class="page-actions">
          <a class="button" href="/">Status</a>
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
    <footer class="footer">
      <span>Authenticated operator area</span>
      <a href="/">Return to read-only status</a>
    </footer>
  </div>
</body>
</html>
"""
    return document.encode("utf-8")


def _admin_tab(*, label: str, href: str, active: bool) -> str:
    active_class = " is-active" if active else ""
    current = ' aria-current="page"' if active else ""
    return (
        f'<a class="admin-tab{active_class}" href="{html.escape(href, quote=True)}"{current}>'
        f"{html.escape(label)}</a>"
    )
