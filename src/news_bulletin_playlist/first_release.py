"""One-time first-release provisioning for the trusted-LAN development flow."""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from news_bulletin_playlist.config import ConfigError, load_config
from news_bulletin_playlist.spotify.client import SpotifyClient

DEFAULT_CONFIG_FILENAME = "news-bulletin-playlist.yaml"
FIRST_PLAYLIST_NAME = "Noticias 48h · España"
FIRST_PLAYLIST_DESCRIPTION = (
    "Boletines en español de SER, RNE, Onda Cero y CNN 5 Cosas. "
    "Actualización automática con las últimas 48 horas, ordenadas de más reciente a más antigua."
)
_SPOTIFY_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9]{22}$")


class PlaylistCreator(Protocol):
    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]: ...


PlaylistCreatorFactory = Callable[[str], PlaylistCreator]


@dataclass(frozen=True, slots=True)
class FirstReleaseProvisioningResult:
    playlist_id: str
    config_path: Path


class FirstReleaseProvisioningError(RuntimeError):
    """Safe user-facing first-release provisioning failure."""

    def __init__(self, message: str, *, playlist_id: str | None = None) -> None:
        super().__init__(message)
        self.playlist_id = playlist_id


def provision_first_release(
    data_dir: Path,
    access_token: str,
    *,
    client_factory: PlaylistCreatorFactory | None = None,
) -> FirstReleaseProvisioningResult:
    """Create the first private Spotify playlist and durable engine config exactly once."""
    config_path = data_dir / DEFAULT_CONFIG_FILENAME
    if config_path.exists():
        raise FirstReleaseProvisioningError("Engine configuration already exists; provisioning refused")

    factory = client_factory or (lambda token: SpotifyClient(token))
    client = factory(access_token)
    response = client.create_private_playlist(
        FIRST_PLAYLIST_NAME,
        description=FIRST_PLAYLIST_DESCRIPTION,
    )
    playlist_id = _playlist_id(response)
    document = first_release_config_yaml(playlist_id)
    try:
        _write_validated_no_replace(config_path, document)
    except (OSError, ConfigError, FirstReleaseProvisioningError) as exc:
        raise FirstReleaseProvisioningError(
            "Spotify playlist was created but engine configuration could not be saved",
            playlist_id=playlist_id,
        ) from exc
    return FirstReleaseProvisioningResult(playlist_id=playlist_id, config_path=config_path)


def first_release_config_yaml(playlist_id: str) -> str:
    """Render the reviewed Spain/Spanish first-release configuration."""
    if _SPOTIFY_PLAYLIST_ID.fullmatch(playlist_id) is None:
        raise FirstReleaseProvisioningError("Spotify returned an invalid playlist identifier")
    document: dict[str, Any] = {
        "schema_version": 1,
        "sources": [
            {
                "id": "ser",
                "display_name": "Cadena SER",
                "countries": ["ES"],
                "languages": ["es"],
                "timezone": "Europe/Madrid",
                "enabled": True,
                "parser_id": "ser",
                "endpoint_url": "https://fapi-top.prisasd.com/podcast/playser/boletines.xml",
                "external_references": [
                    {
                        "system": "spotify",
                        "resource_type": "show",
                        "external_id": "4EwwdoHHYmbt49UXODQMpi",
                    }
                ],
            },
            {
                "id": "rne",
                "display_name": "Radio Nacional de España",
                "countries": ["ES"],
                "languages": ["es"],
                "timezone": "Europe/Madrid",
                "enabled": True,
                "parser_id": "rne",
                "endpoint_url": "https://api.rtve.es/api/adapter/programas/1750/audios.rss",
                "external_references": [
                    {
                        "system": "spotify",
                        "resource_type": "show",
                        "external_id": "0UgidTKsoaHiHDARuPQNW1",
                    }
                ],
            },
            {
                "id": "ondacero",
                "display_name": "Onda Cero",
                "countries": ["ES"],
                "languages": ["es"],
                "timezone": "Europe/Madrid",
                "enabled": True,
                "parser_id": "ondacero",
                "endpoint_url": (
                    "https://www.ondacero.es/rss/podcast/mount/"
                    "ATRESMEDIA_LAS_NOTICIAS_EN_ONDA_CERO_P/fastly"
                ),
                "external_references": [
                    {
                        "system": "spotify",
                        "resource_type": "show",
                        "external_id": "0tjEexypyczHXW9vE3SU3P",
                    }
                ],
            },
            {
                "id": "cnn",
                "display_name": "CNN 5 Cosas",
                "countries": ["US"],
                "languages": ["es"],
                "timezone": "America/New_York",
                "enabled": True,
                "parser_id": "cnn",
                "endpoint_url": "https://feeds.megaphone.fm/WMHY5696831164",
                "external_references": [
                    {
                        "system": "spotify",
                        "resource_type": "show",
                        "external_id": "0vDgnorbpBr65YZzFVVouE",
                    }
                ],
            },
            {
                "id": "cope",
                "display_name": "COPE",
                "countries": ["ES"],
                "languages": ["es"],
                "timezone": "Europe/Madrid",
                "enabled": False,
                "parser_id": "cope",
            },
        ],
        "playlists": [
            {
                "id": "spain_spanish_news",
                "display_name": FIRST_PLAYLIST_NAME,
                "description": FIRST_PLAYLIST_DESCRIPTION,
                "countries": ["ES"],
                "languages": ["es"],
                "enabled": True,
                "source_selection": {"explicit": ["ser", "rne", "ondacero", "cnn"]},
                "destination": {"adapter_id": "spotify", "external_id": playlist_id},
            }
        ],
    }
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _playlist_id(payload: dict[str, Any]) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or _SPOTIFY_PLAYLIST_ID.fullmatch(value) is None:
        raise FirstReleaseProvisioningError("Spotify returned an invalid playlist identifier")
    return value


def _write_validated_no_replace(path: Path, document: str) -> None:
    if path.exists():
        raise FirstReleaseProvisioningError("Engine configuration already exists; provisioning refused")
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        load_config(temp_path)
        try:
            os.link(temp_path, path)
        except FileExistsError as exc:
            raise FirstReleaseProvisioningError(
                "Engine configuration already exists; provisioning refused"
            ) from exc
        _fsync_directory(path.parent)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
