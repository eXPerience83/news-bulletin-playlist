from pathlib import Path

path = Path("src/news_bulletin_playlist/spotify/auth.py")
text = path.read_text(encoding="utf-8")
text = text.replace(
    "            self.scopes = PRODUCTION_REQUESTED_SCOPES\n"
    "            self.required_scopes = PRODUCTION_SCOPES\n",
    "            self.scopes: tuple[str, ...] = PRODUCTION_REQUESTED_SCOPES\n"
    "            self.required_scopes: tuple[str, ...] = PRODUCTION_SCOPES\n",
    1,
)
path.write_text(text, encoding="utf-8")
