from pathlib import Path

path = Path("src/news_bulletin_playlist/spotify/auth.py")
text = path.read_text(encoding="utf-8")

old = '''PRODUCTION_SCOPES = (
    "playlist-read-private",
    "playlist-modify-private",
    "playlist-modify-public",
)
'''
new = '''PRODUCTION_SCOPES = (
    "playlist-read-private",
    "playlist-modify-private",
    "playlist-modify-public",
)
PRODUCTION_REQUESTED_SCOPES = (*PRODUCTION_SCOPES, "ugc-image-upload")
'''
if old not in text:
    raise SystemExit("scope constants fragment not found")
text = text.replace(old, new, 1)

old = '''        transport: SpotifyTokenTransport | None = None,
        scopes: Sequence[str] = PRODUCTION_SCOPES,
    ) -> None:
'''
new = '''        transport: SpotifyTokenTransport | None = None,
        scopes: Sequence[str] | None = None,
        required_scopes: Sequence[str] | None = None,
    ) -> None:
'''
if old not in text:
    raise SystemExit("constructor signature fragment not found")
text = text.replace(old, new, 1)

old = '''        self.store = store
        self.transport = transport if transport is not None else SpotifyAccountsClient()
        self.scopes = tuple(scopes)
        if not self.scopes or any(not scope.strip() for scope in self.scopes):
            raise SpotifyAuthConfigurationError("Spotify authorization scopes are invalid")
'''
new = '''        self.store = store
        self.transport = transport if transport is not None else SpotifyAccountsClient()
        if scopes is None:
            self.scopes = PRODUCTION_REQUESTED_SCOPES
            self.required_scopes = PRODUCTION_SCOPES
        else:
            self.scopes = tuple(scopes)
            self.required_scopes = tuple(required_scopes or self.scopes)
        if not self.scopes or any(not scope.strip() for scope in self.scopes):
            raise SpotifyAuthConfigurationError("Spotify authorization scopes are invalid")
        if not self.required_scopes or any(
            not scope.strip() for scope in self.required_scopes
        ):
            raise SpotifyAuthConfigurationError("Spotify required scopes are invalid")
        if not set(self.required_scopes).issubset(self.scopes):
            raise SpotifyAuthConfigurationError(
                "Spotify required scopes must be included in requested scopes"
            )
'''
if old not in text:
    raise SystemExit("constructor body fragment not found")
text = text.replace(old, new, 1)

count = text.count("_require_scopes(token.granted_scopes, self.scopes)")
if count != 2:
    raise SystemExit(f"expected 2 required-scope checks, found {count}")
text = text.replace(
    "_require_scopes(token.granted_scopes, self.scopes)",
    "_require_scopes(token.granted_scopes, self.required_scopes)",
)

path.write_text(text, encoding="utf-8")
# Trigger the temporary workflow after it exists.
