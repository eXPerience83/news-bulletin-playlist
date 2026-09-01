from pathlib import Path

path = Path("src/news_bulletin_playlist/managed_admin_web.py")
text = path.read_text(encoding="utf-8")
old = '      <p class="muted">Reconnect Spotify once for image permission, then apply Spotify metadata and cover.</p>\n'
new = (
    '      <p class="muted">Reconnect Spotify once for image permission,\n'
    '        then apply Spotify metadata and cover.</p>\n'
)
if old not in text:
    raise SystemExit("cover permission hint not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
