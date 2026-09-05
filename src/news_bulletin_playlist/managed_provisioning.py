"""Crash-safe local journal for one in-progress managed playlist activation."""

from __future__ import annotations

import json
import os
import secrets
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from news_bulletin_playlist.managed_duration import validate_persisted_duration_seconds
from news_bulletin_playlist.models import PlaylistId, SourceId

MANAGED_PROVISIONING_FILENAME = "managed-playlist-provisioning.json"
_SCHEMA_VERSION = 1


class ProvisioningJournalError(ValueError):
    """The local activation journal is malformed or cannot be persisted safely."""


class ProvisioningState(StrEnum):
    REQUEST_STARTED = "request_started"
    DESTINATION_KNOWN = "destination_known"


@dataclass(frozen=True, slots=True)
class ProvisioningIntent:
    state: ProvisioningState
    template_id: PlaylistId
    display_name: str
    description: str
    cover_id: str
    source_ids: tuple[SourceId, ...]
    retention_hours: int
    max_episodes: int
    max_duration_seconds: int
    destination_id: str | None = None

    def __post_init__(self) -> None:
        if not self.display_name.strip() or not self.cover_id.strip() or not self.source_ids:
            raise ValueError("provisioning intent is incomplete")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("provisioning intent source_ids contain duplicates")
        if self.retention_hours <= 0 or self.max_episodes <= 0:
            raise ValueError(
                "provisioning intent retention and maximum episode values must be positive"
            )
        validate_persisted_duration_seconds(self.max_duration_seconds)
        if self.state is ProvisioningState.REQUEST_STARTED and self.destination_id is not None:
            raise ValueError("request-started provisioning intent cannot have a destination")
        if self.state is ProvisioningState.DESTINATION_KNOWN and (
            self.destination_id is None or not self.destination_id.strip()
        ):
            raise ValueError("destination-known provisioning intent requires a destination")

    def with_destination(self, destination_id: str) -> ProvisioningIntent:
        value = destination_id.strip()
        if not value:
            raise ValueError("provisioning destination must not be empty")
        return ProvisioningIntent(
            state=ProvisioningState.DESTINATION_KNOWN,
            template_id=self.template_id,
            display_name=self.display_name,
            description=self.description,
            cover_id=self.cover_id,
            source_ids=self.source_ids,
            retention_hours=self.retention_hours,
            max_episodes=self.max_episodes,
            max_duration_seconds=self.max_duration_seconds,
            destination_id=value,
        )


class ProvisioningJournal:
    """Owner-only, atomic installation state for a potentially-created destination."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ProvisioningIntent | None:
        if self.path.is_symlink():
            raise ProvisioningJournalError("provisioning journal is not a regular file")
        if not self.path.exists():
            return None
        if not self.path.is_file():
            raise ProvisioningJournalError("provisioning journal is not a regular file")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProvisioningJournalError("provisioning journal is unreadable") from exc
        return _parse_intent(payload)

    def save(self, intent: ProvisioningIntent) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "state": intent.state.value,
            "template_id": str(intent.template_id),
            "display_name": intent.display_name,
            "description": intent.description,
            "cover_id": intent.cover_id,
            "source_ids": [str(source_id) for source_id in intent.source_ids],
            "retention_hours": intent.retention_hours,
            "max_episodes": intent.max_episodes,
            "max_duration_seconds": intent.max_duration_seconds,
            "destination_id": intent.destination_id,
        }
        _parse_intent(payload)
        document = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ProvisioningJournalError("provisioning journal is not a regular file")
        temporary = parent / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            _fsync_directory(parent)
        except OSError as exc:
            raise ProvisioningJournalError("provisioning journal could not be saved") from exc
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def clear(self) -> None:
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ProvisioningJournalError("provisioning journal is not a regular file")
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ProvisioningJournalError("provisioning journal could not be cleared") from exc
        try:
            _fsync_directory(self.path.parent)
        except OSError as exc:
            raise ProvisioningJournalError(
                "provisioning journal deletion could not be synchronized"
            ) from exc


def _parse_intent(value: object) -> ProvisioningIntent:
    if not isinstance(value, dict):
        raise ProvisioningJournalError("provisioning journal must contain an object")
    expected = {
        "schema_version",
        "state",
        "template_id",
        "display_name",
        "description",
        "cover_id",
        "source_ids",
        "retention_hours",
        "max_episodes",
        "max_duration_seconds",
        "destination_id",
    }
    if set(value) != expected or value.get("schema_version") != _SCHEMA_VERSION:
        raise ProvisioningJournalError("provisioning journal has an unsupported schema")
    try:
        state = ProvisioningState(_nonempty_string(value["state"]))
        source_values = value["source_ids"]
        if not isinstance(source_values, list):
            raise ValueError
        return ProvisioningIntent(
            state=state,
            template_id=PlaylistId(_nonempty_string(value["template_id"])),
            display_name=_nonempty_string(value["display_name"]),
            description=_string(value["description"]),
            cover_id=_nonempty_string(value["cover_id"]),
            source_ids=tuple(SourceId(_nonempty_string(item)) for item in source_values),
            retention_hours=_positive_integer(value["retention_hours"]),
            max_episodes=_positive_integer(value["max_episodes"]),
            max_duration_seconds=validate_persisted_duration_seconds(value["max_duration_seconds"]),
            destination_id=(
                None
                if value["destination_id"] is None
                else _nonempty_string(value["destination_id"])
            ),
        )
    except (KeyError, ValueError) as exc:
        raise ProvisioningJournalError("provisioning journal contains invalid values") from exc


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _nonempty_string(value: object) -> str:
    result = _string(value).strip()
    if not result:
        raise ValueError
    return result


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
