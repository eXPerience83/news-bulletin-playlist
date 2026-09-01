from pathlib import Path

managed = Path("src/news_bulletin_playlist/managed_admin.py")
text = managed.read_text(encoding="utf-8")
old = '''        spotify_metadata_updated = False
        client = None if access_token is None else self.client_factory(access_token)
        if metadata_changed:
            if client is None:
                raise ManagedAdminError(
                    "Spotify must be connected to change playlist name or description"
                )
            client.change_playlist_details(
                current.destination.external_id,
                name=updated.display_name,
                description=render_spotify_description(updated.description),
            )
            spotify_metadata_updated = True
'''
new = '''        spotify_metadata_updated = False
        if metadata_changed:
            if access_token is None:
                raise ManagedAdminError(
                    "Spotify must be connected to change playlist name or description"
                )
            self.client_factory(access_token).change_playlist_details(
                current.destination.external_id,
                name=updated.display_name,
                description=render_spotify_description(updated.description),
            )
            spotify_metadata_updated = True
'''
if old not in text:
    raise SystemExit("managed update client fragment not found")
text = text.replace(old, new, 1)
old = '''            if spotify_metadata_updated:
                raise SpotifyPlaylistPersistenceError(
                    current.destination.external_id
                ) from exc
            raise
        if client is not None:
            self._best_effort_cover_upload(
                client,
                current.destination.external_id,
                updated.cover_id,
            )
        return updated

    def set_enabled'''
new = '''            if spotify_metadata_updated:
                raise SpotifyPlaylistPersistenceError(
                    current.destination.external_id
                ) from exc
            raise
        return updated

    def sync_spotify_metadata_and_cover(
        self,
        playlist_id: PlaylistId | str,
        *,
        access_token: str,
    ) -> ManagedPlaylist:
        state = self.store.load()
        current = self._managed(state, playlist_id)
        client = self.client_factory(access_token)
        client.change_playlist_details(
            current.destination.external_id,
            name=current.display_name,
            description=render_spotify_description(current.description),
        )
        self._best_effort_cover_upload(
            client,
            current.destination.external_id,
            current.cover_id,
        )
        return current

    def set_enabled'''
if old not in text:
    raise SystemExit("managed explicit sync insertion fragment not found")
text = text.replace(old, new, 1)
managed.write_text(text, encoding="utf-8")

runtime = Path("src/news_bulletin_playlist/engine_runtime.py")
text = runtime.read_text(encoding="utf-8")
old = '''_MANAGED_POST_PATHS = {
    "/admin/playlists/activate",
    "/admin/playlists/update",
    "/admin/playlists/stop",
}
'''
new = '''_MANAGED_POST_PATHS = {
    "/admin/playlists/activate",
    "/admin/playlists/update",
    "/admin/playlists/sync",
    "/admin/playlists/stop",
}
'''
if old not in text:
    raise SystemExit("runtime managed paths fragment not found")
text = text.replace(old, new, 1)

old = '''            with synchronization.hold():
                if path == "/admin/playlists/activate":
                    self._activate_managed_playlist(service, form)
                elif path == "/admin/playlists/update":
                    self._update_managed_playlist(service, form)
                else:
                    service.stop_managing(playlist_id_from_form(form))
                configured = any(playlist.enabled for playlist in service.snapshot().managed)
            lifecycle.reconcile(configured=configured)
            self.__class__.engine_scheduler = lifecycle.scheduler
'''
new = '''            configuration_changed = path != "/admin/playlists/sync"
            with synchronization.hold():
                if path == "/admin/playlists/activate":
                    self._activate_managed_playlist(service, form)
                elif path == "/admin/playlists/update":
                    self._update_managed_playlist(service, form)
                elif path == "/admin/playlists/sync":
                    self._sync_managed_playlist(service, form)
                else:
                    service.stop_managing(playlist_id_from_form(form))
                configured = any(playlist.enabled for playlist in service.snapshot().managed)
            if configuration_changed:
                lifecycle.reconcile(configured=configured)
                self.__class__.engine_scheduler = lifecycle.scheduler
'''
if old not in text:
    raise SystemExit("runtime managed post dispatch fragment not found")
text = text.replace(old, new, 1)

old = '''        access_token: str | None = None
        auth = self.managed_admin_auth
        if metadata_changed:
            if auth is None:
                raise ManagedAdminError(
                    "Spotify must be connected to change playlist name or description"
                )
            access_token = auth.get_access_token()
        elif auth is not None:
            try:
                access_token = auth.get_access_token()
            except SpotifyAuthError:
                # Local source/pause changes remain available when optional cover sync cannot auth.
                access_token = None
'''
new = '''        access_token: str | None = None
        if metadata_changed:
            auth = self.managed_admin_auth
            if auth is None:
                raise ManagedAdminError(
                    "Spotify must be connected to change playlist name or description"
                )
            access_token = auth.get_access_token()
'''
if old not in text:
    raise SystemExit("runtime normal Save token fragment not found")
text = text.replace(old, new, 1)

marker = '''    def _managed_error(self, status: HTTPStatus, message: str) -> None:
'''
helper = '''    def _sync_managed_playlist(
        self,
        service: ManagedAdminService,
        form: Mapping[str, list[str]],
    ) -> None:
        auth = self.managed_admin_auth
        if auth is None:
            raise ManagedAdminError("Spotify must be connected to apply metadata and cover")
        service.sync_spotify_metadata_and_cover(
            playlist_id_from_form(form),
            access_token=auth.get_access_token(),
        )

''' + marker
if marker not in text:
    raise SystemExit("runtime sync helper insertion point not found")
text = text.replace(marker, helper, 1)
runtime.write_text(text, encoding="utf-8")

web = Path("src/news_bulletin_playlist/managed_admin_web.py")
text = web.read_text(encoding="utf-8")
old = '''      <p class="muted">Last result: {html.escape(result)}</p>
      <p class="muted">Bundled cover is applied on Save when Spotify image permission is granted.</p>
'''
new = '''      <p class="muted">Last result: {html.escape(result)}</p>
      <p class="muted">Reconnect Spotify once for image permission, then apply Spotify metadata and cover.</p>
'''
if old not in text:
    raise SystemExit("web managed hint fragment not found")
text = text.replace(old, new, 1)
old = '''    <button type="submit">Save playlist</button>
  </form>
  <form method="post" action="/admin/playlists/stop">
'''
new = '''    <button type="submit">Save playlist</button>
  </form>
  <form method="post" action="/admin/playlists/sync">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <input type="hidden" name="playlist_id" value="{html.escape(str(playlist.id), quote=True)}">
    <button type="submit">Apply Spotify metadata &amp; cover</button>
  </form>
  <form method="post" action="/admin/playlists/stop">
'''
if old not in text:
    raise SystemExit("web sync form insertion point not found")
text = text.replace(old, new, 1)
web.write_text(text, encoding="utf-8")

cover_tests = Path("tests/test_playlist_cover_upload.py")
text = cover_tests.read_text(encoding="utf-8")
old = '''def test_save_retries_cover_when_access_token_is_available(tmp_path: Path) -> None:
    client = _CoverClient(fail_cover=True)
    jpeg = b"\\xff\\xd8cover\\xff\\xd9"
    service, managed = _activate(tmp_path, client, cover_loader=lambda _cover_id: jpeg)
    client.fail_cover = False
    client.cover_calls.clear()

    updated = service.update(
        managed.id,
        display_name=managed.display_name,
        description=managed.description,
        cover_id=managed.cover_id,
        source_ids=managed.source_ids,
        enabled=True,
        access_token="reauthorized-token",
    )

    assert updated == managed
    assert client.cover_calls == [("destination", jpeg)]


'''
new = '''def test_explicit_sync_reapplies_metadata_and_retries_cover(tmp_path: Path) -> None:
    client = _CoverClient(fail_cover=True)
    jpeg = b"\\xff\\xd8cover\\xff\\xd9"
    service, managed = _activate(tmp_path, client, cover_loader=lambda _cover_id: jpeg)
    client.fail_cover = False
    client.cover_calls.clear()
    client.update_calls.clear()

    synced = service.sync_spotify_metadata_and_cover(
        managed.id,
        access_token="reauthorized-token",
    )

    assert synced == managed
    assert client.update_calls == [
        (
            "destination",
            managed.display_name,
            __import__("news_bulletin_playlist.managed_admin", fromlist=["render_spotify_description"])
            .render_spotify_description(managed.description),
        )
    ]
    assert client.cover_calls == [("destination", jpeg)]


def test_cover_failure_during_explicit_sync_does_not_fail_metadata_sync(tmp_path: Path) -> None:
    client = _CoverClient()
    jpeg = b"\\xff\\xd8cover\\xff\\xd9"
    service, managed = _activate(tmp_path, client, cover_loader=lambda _cover_id: jpeg)
    client.fail_cover = True
    client.cover_calls.clear()
    client.update_calls.clear()

    synced = service.sync_spotify_metadata_and_cover(managed.id, access_token="token")

    assert synced == managed
    assert len(client.update_calls) == 1
    assert client.cover_calls == [("destination", jpeg)]


'''
if old not in text:
    raise SystemExit("cover test Save retry fragment not found")
text = text.replace(old, new, 1)
cover_tests.write_text(text, encoding="utf-8")
# Trigger the temporary patch workflow after it is configured for this script.
