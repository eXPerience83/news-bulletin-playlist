from pathlib import Path

path = Path("tests/test_managed_admin.py")
text = path.read_text(encoding="utf-8")

text = text.replace(
    "    SpotifyPlaylistProvisioningError,\n)",
    "    SpotifyPlaylistProvisioningError,\n    render_spotify_description,\n)",
)

replacements = {
    'assert factory.client.create_calls == [("Noticias España", "Descripción personalizada")]':
        'assert factory.client.create_calls == [("Noticias España", render_spotify_description("Descripción personalizada"))]',
    'assert factory.client.create_calls == [(template.display_name, template.description)]':
        'assert factory.client.create_calls == [(template.display_name, render_spotify_description(template.description))]',
    '("destination", "Noticias España Ahora", "Descripción nueva")':
        '("destination", "Noticias España Ahora", render_spotify_description("Descripción nueva"))',
    '("destination", "Nuevo nombre", managed.description)':
        '("destination", "Nuevo nombre", render_spotify_description(managed.description))',
}
for old, new in replacements.items():
    if old not in text:
        raise SystemExit(f"missing expected test fragment: {old}")
    text = text.replace(old, new)

path.write_text(text, encoding="utf-8")
