from pathlib import Path

# Spotify auth: request image scope but keep existing playlist scopes mandatory.
auth = Path("src/news_bulletin_playlist/spotify/auth.py")
text = auth.read_text(encoding="utf-8")
if "PRODUCTION_REQUESTED_SCOPES" not in text:
    old = '''PRODUCTION_SCOPES = (\n    "playlist-read-private",\n    "playlist-modify-private",\n    "playlist-modify-public",\n)\n'''
    new = old + 'PRODUCTION_REQUESTED_SCOPES = (*PRODUCTION_SCOPES, "ugc-image-upload")\n'
    if old not in text:
        raise SystemExit("auth scope constants fragment not found")
    text = text.replace(old, new, 1)
if "required_scopes: Sequence[str] | None = None" not in text:
    old = '''        transport: SpotifyTokenTransport | None = None,\n        scopes: Sequence[str] = PRODUCTION_SCOPES,\n    ) -> None:\n'''
    new = '''        transport: SpotifyTokenTransport | None = None,\n        scopes: Sequence[str] | None = None,\n        required_scopes: Sequence[str] | None = None,\n    ) -> None:\n'''
    if old not in text:
        raise SystemExit("auth constructor signature fragment not found")
    text = text.replace(old, new, 1)
    old = '''        self.store = store\n        self.transport = transport if transport is not None else SpotifyAccountsClient()\n        self.scopes = tuple(scopes)\n        if not self.scopes or any(not scope.strip() for scope in self.scopes):\n            raise SpotifyAuthConfigurationError("Spotify authorization scopes are invalid")\n'''
    new = '''        self.store = store\n        self.transport = transport if transport is not None else SpotifyAccountsClient()\n        if scopes is None:\n            self.scopes = PRODUCTION_REQUESTED_SCOPES\n            self.required_scopes = PRODUCTION_SCOPES\n        else:\n            self.scopes = tuple(scopes)\n            self.required_scopes = tuple(required_scopes or self.scopes)\n        if not self.scopes or any(not scope.strip() for scope in self.scopes):\n            raise SpotifyAuthConfigurationError("Spotify authorization scopes are invalid")\n        if not self.required_scopes or any(\n            not scope.strip() for scope in self.required_scopes\n        ):\n            raise SpotifyAuthConfigurationError("Spotify required scopes are invalid")\n        if not set(self.required_scopes).issubset(self.scopes):\n            raise SpotifyAuthConfigurationError(\n                "Spotify required scopes must be included in requested scopes"\n            )\n'''
    if old not in text:
        raise SystemExit("auth constructor body fragment not found")
    text = text.replace(old, new, 1)
if "_require_scopes(token.granted_scopes, self.scopes)" in text:
    text = text.replace(
        "_require_scopes(token.granted_scopes, self.scopes)",
        "_require_scopes(token.granted_scopes, self.required_scopes)",
    )
auth.write_text(text, encoding="utf-8")

# Managed admin: upload bundled covers best-effort after durable state is safe.
managed = Path("src/news_bulletin_playlist/managed_admin.py")
text = managed.read_text(encoding="utf-8")
old = '''    def change_playlist_details(\n        self,\n        playlist_id: str,\n        *,\n        name: str,\n        description: str,\n    ) -> dict[str, Any]: ...\n\n\nPlaylistClientFactory = Callable[[str], PlaylistProvisioningClient]\n'''
new = '''    def change_playlist_details(\n        self,\n        playlist_id: str,\n        *,\n        name: str,\n        description: str,\n    ) -> dict[str, Any]: ...\n\n    def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict[str, Any]: ...\n\n\nPlaylistClientFactory = Callable[[str], PlaylistProvisioningClient]\nCoverAssetLoader = Callable[[str], bytes]\n'''
if old not in text:
    raise SystemExit("managed protocol fragment not found")
text = text.replace(old, new, 1)

old = '''        catalog: BuiltInCatalog = BUILTIN_CATALOG,\n        client_factory: PlaylistClientFactory | None = None,\n    ) -> None:\n        self.store = store\n        self.catalog = catalog\n        self.client_factory = client_factory or SpotifyClient\n'''
new = '''        catalog: BuiltInCatalog = BUILTIN_CATALOG,\n        client_factory: PlaylistClientFactory | None = None,\n        cover_loader: CoverAssetLoader | None = None,\n    ) -> None:\n        self.store = store\n        self.catalog = catalog\n        self.client_factory = client_factory or SpotifyClient\n        self.cover_loader = cover_loader\n'''
if old not in text:
    raise SystemExit("managed constructor fragment not found")
text = text.replace(old, new, 1)

old = '''        try:\n            response = self.client_factory(access_token).create_private_playlist(\n                name,\n                description=render_spotify_description(safe_description),\n            )\n'''
new = '''        client = self.client_factory(access_token)\n        try:\n            response = client.create_private_playlist(\n                name,\n                description=render_spotify_description(safe_description),\n            )\n'''
if old not in text:
    raise SystemExit("managed activate client fragment not found")
text = text.replace(old, new, 1)

old = '''        try:\n            self._save_validated(next_state)\n        except (ManagedStateError, OSError) as exc:\n            raise SpotifyPlaylistProvisioningError(destination_id) from exc\n        return managed\n'''
new = '''        try:\n            self._save_validated(next_state)\n        except (ManagedStateError, OSError) as exc:\n            raise SpotifyPlaylistProvisioningError(destination_id) from exc\n        self._best_effort_cover_upload(client, destination_id, cover)\n        return managed\n'''
if old not in text:
    raise SystemExit("managed activate persistence fragment not found")
text = text.replace(old, new, 1)

old = '''        spotify_metadata_updated = False\n        if metadata_changed:\n            if access_token is None:\n                raise ManagedAdminError(\n                    "Spotify must be connected to change playlist name or description"\n                )\n            self.client_factory(access_token).change_playlist_details(\n                current.destination.external_id,\n                name=updated.display_name,\n                description=render_spotify_description(updated.description),\n            )\n            spotify_metadata_updated = True\n'''
new = '''        spotify_metadata_updated = False\n        client = None if access_token is None else self.client_factory(access_token)\n        if metadata_changed:\n            if client is None:\n                raise ManagedAdminError(\n                    "Spotify must be connected to change playlist name or description"\n                )\n            client.change_playlist_details(\n                current.destination.external_id,\n                name=updated.display_name,\n                description=render_spotify_description(updated.description),\n            )\n            spotify_metadata_updated = True\n'''
if old not in text:
    raise SystemExit("managed update metadata fragment not found")
text = text.replace(old, new, 1)

old = '''            if spotify_metadata_updated:\n                raise SpotifyPlaylistPersistenceError(\n                    current.destination.external_id\n                ) from exc\n            raise\n        return updated\n\n    def set_enabled'''
new = '''            if spotify_metadata_updated:\n                raise SpotifyPlaylistPersistenceError(\n                    current.destination.external_id\n                ) from exc\n            raise\n        if client is not None:\n            self._best_effort_cover_upload(\n                client,\n                current.destination.external_id,\n                updated.cover_id,\n            )\n        return updated\n\n    def set_enabled'''
if old not in text:
    raise SystemExit("managed update persistence fragment not found")
text = text.replace(old, new, 1)

marker = '''    def _replace(self, state: ManagedState, updated: ManagedPlaylist) -> None:\n        self._save_validated(self._state_with_replacement(state, updated))\n'''
helper = '''    def _best_effort_cover_upload(\n        self,\n        client: PlaylistProvisioningClient,\n        playlist_id: str,\n        cover_id: str,\n    ) -> None:\n        if self.cover_loader is None:\n            return\n        try:\n            jpeg_bytes = self.cover_loader(cover_id)\n            client.upload_playlist_cover(playlist_id, jpeg_bytes)\n        except (OSError, ValueError, SpotifyApiError, SpotifyTransportError):\n            # Cover art is product metadata. It must never block playlist state or bulletin sync.\n            return\n\n''' + marker
if marker not in text:
    raise SystemExit("managed helper insertion point not found")
text = text.replace(marker, helper, 1)
managed.write_text(text, encoding="utf-8")

# Runtime: use bundled JPEG loader and opportunistically obtain a token on Save.
runtime = Path("src/news_bulletin_playlist/engine_runtime.py")
text = runtime.read_text(encoding="utf-8")
old = '''        access_token: str | None = None\n        if metadata_changed:\n            auth = self.managed_admin_auth\n            if auth is None:\n                raise ManagedAdminError(\n                    "Spotify must be connected to change playlist name or description"\n                )\n            access_token = auth.get_access_token()\n'''
new = '''        access_token: str | None = None\n        auth = self.managed_admin_auth\n        if metadata_changed:\n            if auth is None:\n                raise ManagedAdminError(\n                    "Spotify must be connected to change playlist name or description"\n                )\n            access_token = auth.get_access_token()\n        elif auth is not None:\n            try:\n                access_token = auth.get_access_token()\n            except SpotifyAuthError:\n                # Local source/pause changes remain available when optional cover sync cannot auth.\n                access_token = None\n'''
if old not in text:
    raise SystemExit("runtime update token fragment not found")
text = text.replace(old, new, 1)

old = '''    return ManagedAdminService(ManagedStateStore(data_dir / MANAGED_STATE_FILENAME))\n\n\ndef _bundled_cover_path(filename: str) -> Path | None:\n'''
new = '''    return ManagedAdminService(\n        ManagedStateStore(data_dir / MANAGED_STATE_FILENAME),\n        cover_loader=_load_bundled_cover,\n    )\n\n\ndef _load_bundled_cover(cover_id: str) -> bytes:\n    cover_path = _bundled_cover_path(f"{cover_id}.jpg")\n    if cover_path is None:\n        raise FileNotFoundError("bundled playlist cover is unavailable")\n    return cover_path.read_bytes()\n\n\ndef _bundled_cover_path(filename: str) -> Path | None:\n'''
if old not in text:
    raise SystemExit("runtime managed service fragment not found")
text = text.replace(old, new, 1)
runtime.write_text(text, encoding="utf-8")

# Web UI: make the migration/reconnect behavior explicit.
web = Path("src/news_bulletin_playlist/managed_admin_web.py")
text = web.read_text(encoding="utf-8")
old = '''      <p class="muted">Last result: {html.escape(result)}</p>\n    </div>\n  </div>\n'''
new = '''      <p class="muted">Last result: {html.escape(result)}</p>\n      <p class="muted">Bundled cover is applied on Save when Spotify image permission is granted.</p>\n    </div>\n  </div>\n'''
if old not in text:
    raise SystemExit("web managed-card fragment not found")
text = text.replace(old, new, 1)
old = '''      <p class="muted">Built-in template · creates a private Spotify playlist</p>\n    </div>\n  </div>\n'''
new = '''      <p class="muted">Built-in template · creates a private Spotify playlist</p>\n      <p class="muted">The bundled cover is uploaded when Spotify grants image permission.</p>\n    </div>\n  </div>\n'''
if old not in text:
    raise SystemExit("web template-card fragment not found")
text = text.replace(old, new, 1)
web.write_text(text, encoding="utf-8")

# Auth tests: default authorization requests image upload while old core grants remain sufficient.
tests = Path("tests/test_spotify_auth.py")
text = tests.read_text(encoding="utf-8")
old = '''from news_bulletin_playlist.spotify.auth import (\n    PRODUCTION_SCOPES,\n'''
new = '''from news_bulletin_playlist.spotify.auth import (\n    PRODUCTION_REQUESTED_SCOPES,\n    PRODUCTION_SCOPES,\n'''
if old not in text:
    raise SystemExit("auth test import fragment not found")
text = text.replace(old, new, 1)
old = '''    assert tuple(params["scope"][0].split()) == PRODUCTION_SCOPES\n'''
new = '''    assert tuple(params["scope"][0].split()) == PRODUCTION_REQUESTED_SCOPES\n    assert "ugc-image-upload" in params["scope"][0].split()\n'''
if old not in text:
    raise SystemExit("auth test scope assertion not found")
text = text.replace(old, new, 1)
tests.write_text(text, encoding="utf-8")
