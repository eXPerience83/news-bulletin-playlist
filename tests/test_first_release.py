from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.config import load_config
from news_bulletin_playlist.first_release import (
    DEFAULT_CONFIG_FILENAME,
    FIRST_PLAYLIST_DESCRIPTION,
    FIRST_PLAYLIST_NAME,
    FirstReleaseProvisioningError,
    provision_first_release,
)

_VALID_PLAYLIST_ID = "1234567890123456789012"


class _FakePlaylistClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:
        self.calls.append((name, description))
        return self.response


def test_first_release_creates_private_destination_config_atomically(tmp_path: Path) -> None:
    client = _FakePlaylistClient({"id": _VALID_PLAYLIST_ID})
    factory_tokens: list[str] = []

    def factory(access_token: str) -> _FakePlaylistClient:
        factory_tokens.append(access_token)
        return client

    access_token = "access-token-sentinel"
    result = provision_first_release(tmp_path, access_token, client_factory=factory)

    assert result.playlist_id == _VALID_PLAYLIST_ID
    assert result.config_path == tmp_path / DEFAULT_CONFIG_FILENAME
    assert factory_tokens == [access_token]
    assert client.calls == [(FIRST_PLAYLIST_NAME, FIRST_PLAYLIST_DESCRIPTION)]
    assert stat.S_IMODE(result.config_path.stat().st_mode) == 0o600

    config = load_config(result.config_path)
    assert [str(source.id) for source in config.sources if source.enabled] == [
        "ser",
        "rne",
        "ondacero",
        "cnn",
    ]
    assert len(config.playlists) == 1
    playlist = config.playlists[0]
    assert str(playlist.id) == "spain_spanish_news"
    assert playlist.display_name == FIRST_PLAYLIST_NAME
    assert playlist.description == FIRST_PLAYLIST_DESCRIPTION
    assert playlist.destination.external_id == _VALID_PLAYLIST_ID
    assert playlist.retention_hours == 48
    assert playlist.max_episodes == 100

    raw = result.config_path.read_text(encoding="utf-8")
    assert access_token not in raw
    assert not list(tmp_path.glob(f".{DEFAULT_CONFIG_FILENAME}.*.tmp"))


def test_existing_config_refuses_before_spotify_client_creation(tmp_path: Path) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_FILENAME
    config_path.write_text("sentinel existing config", encoding="utf-8")
    factory_called = False

    def factory(access_token: str) -> _FakePlaylistClient:
        nonlocal factory_called
        del access_token
        factory_called = True
        raise AssertionError("Spotify client must not be created when config already exists")

    with pytest.raises(FirstReleaseProvisioningError, match="already exists"):
        provision_first_release(tmp_path, "access-token-sentinel", client_factory=factory)

    assert not factory_called
    assert config_path.read_text(encoding="utf-8") == "sentinel existing config"


def test_invalid_spotify_playlist_id_does_not_create_config(tmp_path: Path) -> None:
    client = _FakePlaylistClient({"id": "invalid-id"})

    with pytest.raises(FirstReleaseProvisioningError, match="invalid playlist identifier"):
        provision_first_release(
            tmp_path,
            "access-token-sentinel",
            client_factory=lambda access_token: client,
        )

    assert not (tmp_path / DEFAULT_CONFIG_FILENAME).exists()


def test_local_persistence_failure_reports_created_playlist_id_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _FakePlaylistClient({"id": _VALID_PLAYLIST_ID})
    access_token = "access-token-sentinel"

    def fail_write(path: Path, document: str) -> None:
        del path, document
        raise OSError("disk unavailable")

    monkeypatch.setattr(
        "news_bulletin_playlist.first_release._write_validated_no_replace",
        fail_write,
    )

    with pytest.raises(FirstReleaseProvisioningError) as raised:
        provision_first_release(
            tmp_path,
            access_token,
            client_factory=lambda token: client,
        )

    assert raised.value.playlist_id == _VALID_PLAYLIST_ID
    assert "could not be saved" in str(raised.value)
    assert access_token not in str(raised.value)
    captured = capsys.readouterr()
    assert access_token not in captured.out
    assert access_token not in captured.err
    assert not (tmp_path / DEFAULT_CONFIG_FILENAME).exists()
