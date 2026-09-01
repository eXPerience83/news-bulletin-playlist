from pathlib import Path

web = Path("src/news_bulletin_playlist/managed_admin_web.py")
text = web.read_text(encoding="utf-8")
old = '      <p class="muted">Reconnect Spotify once for image permission, then apply Spotify metadata and cover.</p>\n'
new = (
    '      <p class="muted">Reconnect Spotify once for image permission, then apply Spotify '\
    'metadata and cover.</p>\n'
)
if old not in text:
    raise SystemExit("managed admin cover hint not found")
web.write_text(text.replace(old, new, 1), encoding="utf-8")

client = Path("src/news_bulletin_playlist/spotify/client.py")
text = client.read_text(encoding="utf-8")
old = '''        if not jpeg_bytes.startswith(b"\\xff\\xd8"):\n            raise ValueError("playlist cover must be a JPEG image")\n'''
new = '''        if not jpeg_bytes.startswith(b"\\xff\\xd8") or not jpeg_bytes.endswith(b"\\xff\\xd9"):\n            raise ValueError("playlist cover must be a complete JPEG image")\n'''
if old not in text:
    raise SystemExit("JPEG validation fragment not found")
text = text.replace(old, new, 1)
old = '''        if json_body is not None:\n            data = json.dumps(json_body).encode("utf-8")\n        else:\n            data = raw_body\n'''
new = '''        data = json.dumps(json_body).encode("utf-8") if json_body is not None else raw_body\n'''
if old not in text:
    raise SystemExit("Spotify request data fragment not found")
client.write_text(text.replace(old, new, 1), encoding="utf-8")
