"""Server-rendered administration login page."""

from __future__ import annotations

from news_bulletin_playlist.web_ui import WEB_UI_STYLES

_LOGIN_STYLES = """
.login-shell {
  width: min(30rem, calc(100% - 1.25rem));
  margin: 0 auto;
  padding: 1rem 0 4rem;
}

.login-topbar {
  min-height: 4rem;
}

.login-main {
  padding-top: clamp(2.5rem, 10vw, 6.5rem);
}

.login-card {
  padding: clamp(1.2rem, 5vw, 2rem);
}

.login-card h1 {
  margin: 0;
  font-size: clamp(2.2rem, 7vw, 3.6rem);
  line-height: 1;
  letter-spacing: -0.055em;
}

.login-card .hero-copy {
  margin-bottom: 1.4rem;
}

.login-card label {
  display: block;
  margin-top: 0.9rem;
  font-size: 0.9rem;
  font-weight: 720;
}

.login-card input[type="text"],
.login-card input[type="password"] {
  width: 100%;
  margin-top: 0.35rem;
  padding: 0.72rem 0.78rem;
  border: 1px solid var(--border-strong);
  border-radius: 0.72rem;
  background: var(--bg-soft);
  color: var(--text);
  font: inherit;
}

.login-actions {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  margin-top: 1.2rem;
  flex-wrap: wrap;
}

.login-error,
.login-warning {
  margin: 1rem 0 0;
  padding: 0.82rem 0.95rem;
  border: 1px solid var(--border);
  border-radius: 0.8rem;
}

.login-error {
  border-color: color-mix(in srgb, var(--danger) 35%, transparent);
  background: var(--danger-soft);
}

.login-warning {
  border-color: color-mix(in srgb, var(--warning) 35%, transparent);
  background: var(--warning-soft);
}
"""


def render_admin_login_page(*, invalid_credentials: bool, lan_mode: bool) -> bytes:
    """Render the small dedicated username/password login surface."""

    error = (
        '<div class="login-error" role="alert">Username or password is incorrect.</div>'
        if invalid_credentials
        else ""
    )
    warning = (
        '<div class="login-warning"><strong>Trusted LAN mode.</strong> '
        "Credentials and session traffic use local HTTP without TLS in this deployment. "
        "Keep port 8788 only on a trusted private network.</div>"
        if lan_mode
        else ""
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>Sign in · News Bulletin Playlists</title>
  <style>{WEB_UI_STYLES}{_LOGIN_STYLES}</style>
</head>
<body>
  <div class="login-shell">
    <header class="topbar login-topbar">
      <a class="brand" href="/" aria-label="News Bulletin Playlists status">
        <span class="brand-mark" aria-hidden="true">NB</span>
        <span>News Bulletin Playlists</span>
      </a>
      <a class="button" href="/">Status</a>
    </header>
    <main class="login-main">
      <section class="card login-card" aria-labelledby="login-title">
        <p class="eyebrow">Administration</p>
        <h1 id="login-title">Sign in</h1>
        <p class="hero-copy">Use the administration credentials configured for this installation.</p>
        {warning}
        {error}
        <form method="post" action="/admin/login">
          <label for="admin-username">Username</label>
          <input id="admin-username" name="username" type="text" value="admin"
                 autocomplete="username" required>
          <label for="admin-password">Password</label>
          <input id="admin-password" name="password" type="password"
                 autocomplete="current-password" required>
          <div class="login-actions">
            <button class="button button-primary" type="submit">Sign in</button>
            <a class="button" href="/">Back to Status</a>
          </div>
        </form>
      </section>
    </main>
  </div>
</body>
</html>
"""
    return document.encode("utf-8")
