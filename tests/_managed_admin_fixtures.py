"""Offline managed playlist service, fake Spotify client and factory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_state import ManagedStateStore


class _FakeSpotifyClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.create_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, str, str]] = []

    def create_playlist(
        self, name: str, *, public: bool = True, description: str = ""
    ) -> dict[str, Any]:
        assert public is True
        self.create_calls.append((name, description))
        return self.responses.pop(0)

    def change_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        self.update_calls.append((playlist_id, name, description))
        return {}


class _Factory:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.client = _FakeSpotifyClient(responses)
        self.tokens: list[str] = []

    def __call__(self, access_token: str) -> _FakeSpotifyClient:
        self.tokens.append(access_token)
        return self.client


def _service(
    tmp_path: Path,
    responses: list[dict[str, Any]],
) -> tuple[ManagedAdminService, _Factory]:
    factory = _Factory(responses)
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=factory,
    )
    return service, factory
