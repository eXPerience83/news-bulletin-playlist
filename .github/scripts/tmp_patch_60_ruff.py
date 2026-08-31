from pathlib import Path

path = Path("tests/test_managed_admin.py")
text = path.read_text(encoding="utf-8")
text = text.replace(
    '    assert factory.client.create_calls == [("Noticias España", render_spotify_description("Descripción personalizada"))]\n',
    '    assert factory.client.create_calls == [\n'
    '        ("Noticias España", render_spotify_description("Descripción personalizada"))\n'
    '    ]\n',
)
text = text.replace(
    '    assert factory.client.create_calls == [(template.display_name, render_spotify_description(template.description))]\n',
    '    assert factory.client.create_calls == [\n'
    '        (template.display_name, render_spotify_description(template.description))\n'
    '    ]\n',
)
path.write_text(text, encoding="utf-8")
