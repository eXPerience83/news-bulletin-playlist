from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text()
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected exactly one match")
    target.write_text(text.replace(old, new))


replace_once(
    "src/news_bulletin_playlist/managed_admin.py",
    "from news_bulletin_playlist.spotify.client import SpotifyClient, SpotifyTransportError\n",
    "from news_bulletin_playlist.spotify.client import (\n    SpotifyApiError,\n    SpotifyClient,\n    SpotifyTransportError,\n)\n",
)
replace_once(
    "src/news_bulletin_playlist/managed_admin.py",
    '''        super().__init__(\n            "Spotify playlist creation outcome is unknown because the request failed in transit; "\n            "inspect Spotify for a newly created playlist before retrying"\n        )\n''',
    '''        super().__init__(\n            "Spotify playlist creation outcome is unknown because Spotify did not confirm the "\n            "result; inspect Spotify for a newly created playlist before retrying"\n        )\n''',
)
replace_once(
    "src/news_bulletin_playlist/managed_admin.py",
    '''        try:\n            response = self.client_factory(access_token).create_private_playlist(\n                name,\n                description=safe_description,\n            )\n        except SpotifyTransportError as exc:\n            raise SpotifyPlaylistCreationUncertainError() from exc\n        destination_id = _spotify_playlist_id(response)\n''',
    '''        try:\n            response = self.client_factory(access_token).create_private_playlist(\n                name,\n                description=safe_description,\n            )\n        except SpotifyApiError as exc:\n            if exc.status < 500:\n                raise\n            raise SpotifyPlaylistCreationUncertainError() from exc\n        except SpotifyTransportError as exc:\n            raise SpotifyPlaylistCreationUncertainError() from exc\n        try:\n            destination_id = _spotify_playlist_id(response)\n        except ManagedAdminError as exc:\n            raise SpotifyPlaylistCreationUncertainError() from exc\n''',
)

replace_once(
    "tests/test_managed_admin.py",
    "from news_bulletin_playlist.spotify.client import SpotifyTransportError\n",
    "from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError\n",
)
replace_once(
    "tests/test_managed_admin.py",
    '''class _FailingCreateSpotifyClient(_FakeSpotifyClient):\n    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:\n        self.create_calls.append((name, description))\n        raise SpotifyTransportError("simulated create transport failure")\n\n\n''',
    '''class _FailingCreateSpotifyClient(_FakeSpotifyClient):\n    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:\n        self.create_calls.append((name, description))\n        raise SpotifyTransportError("simulated create transport failure")\n\n\nclass _FailingCreateSpotify5xxClient(_FakeSpotifyClient):\n    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:\n        self.create_calls.append((name, description))\n        raise SpotifyApiError(503, "simulated create server failure")\n\n\n''',
)
replace_once(
    "tests/test_managed_admin.py",
    '''def test_spotify_creation_persistence_failure_surfaces_recoverable_destination_id(\n''',
    '''def test_spotify_creation_5xx_failure_is_treated_as_uncertain(tmp_path: Path) -> None:\n    factory = _Factory([])\n    factory.client = _FailingCreateSpotify5xxClient([])\n    service = ManagedAdminService(\n        ManagedStateStore(tmp_path / "managed-state.json"),\n        client_factory=factory,\n    )\n    template = BUILTIN_CATALOG.playlist("spain_spanish_news")\n\n    with pytest.raises(SpotifyPlaylistCreationUncertainError):\n        service.activate(\n            template_id=template.id,\n            display_name=template.display_name,\n            description=template.description,\n            cover_id=template.cover_id,\n            source_ids=template.default_source_ids,\n            access_token="create-token-sentinel",\n        )\n\n    assert service.snapshot().managed == ()\n\n\ndef test_spotify_creation_invalid_success_response_is_treated_as_uncertain(\n    tmp_path: Path,\n) -> None:\n    service, _ = _service(tmp_path, [{}])\n    template = BUILTIN_CATALOG.playlist("spain_spanish_news")\n\n    with pytest.raises(SpotifyPlaylistCreationUncertainError):\n        service.activate(\n            template_id=template.id,\n            display_name=template.display_name,\n            description=template.description,\n            cover_id=template.cover_id,\n            source_ids=template.default_source_ids,\n            access_token="create-token-sentinel",\n        )\n\n    assert service.snapshot().managed == ()\n\n\ndef test_spotify_creation_persistence_failure_surfaces_recoverable_destination_id(\n''',
)
