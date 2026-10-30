"""Conservative, read-only installation-identity preflight (issue #552).

This module deliberately does not hook into startup or create a marker. An
empty-root observation is only a candidate for future atomic initialization,
not permission to activate presets or change managed-state semantics.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

INSTALLATION_IDENTITY_FILENAME = "installation-identity.json"
INSTALLATION_IDENTITY_SCHEMA_VERSION = 1


class InstallationIdentityError(ValueError):
    """Identity evidence is unreadable or ambiguous; never infer opt-in mode."""


class InstallationDisposition(StrEnum):
    EMPTY_UNCLAIMED = "empty_unclaimed"
    LEGACY_COMPATIBLE = "legacy_compatible"
    CLAIMED_LEGACY = "claimed_legacy"
    CLAIMED_OPT_IN = "claimed_opt_in"


@dataclass(frozen=True, slots=True)
class InstallationInspection:
    disposition: InstallationDisposition
    installation_id: str | None = None


def inspect_installation_root(data_dir: Path) -> InstallationInspection:
    """Inspect evidence without creating files, resolving symlinks or modifying state.

    A root with any existing entry and no identity marker is legacy-compatible.
    This includes a SQLite database with no managed-state JSON, OAuth files,
    pending journals, unknown files, subdirectories and broken symlinks. Call
    this before storage initialization; an empty observation is not a lock.
    """
    try:
        root_stat = data_dir.lstat()
    except FileNotFoundError:
        return InstallationInspection(InstallationDisposition.EMPTY_UNCLAIMED)
    except OSError as exc:
        raise InstallationIdentityError("cannot inspect installation data root") from exc

    if not stat.S_ISDIR(root_stat.st_mode):
        raise InstallationIdentityError("installation data root is not a real directory")

    marker = data_dir / INSTALLATION_IDENTITY_FILENAME
    try:
        marker_stat = marker.lstat()
    except FileNotFoundError:
        marker_stat = None
    except OSError as exc:
        raise InstallationIdentityError("cannot inspect installation identity marker") from exc

    if marker_stat is not None:
        if not stat.S_ISREG(marker_stat.st_mode):
            raise InstallationIdentityError("installation identity marker is not a regular file")
        return _read_identity(marker)

    try:
        contains_entries = any(data_dir.iterdir())
    except OSError as exc:
        raise InstallationIdentityError("cannot enumerate installation data root") from exc
    if contains_entries:
        return InstallationInspection(InstallationDisposition.LEGACY_COMPATIBLE)
    return InstallationInspection(InstallationDisposition.EMPTY_UNCLAIMED)


def _read_identity(marker: Path) -> InstallationInspection:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise InstallationIdentityError("invalid installation identity marker") from exc

    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "installation_id",
        "mode",
    }:
        raise InstallationIdentityError("installation identity marker has unexpected fields")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != INSTALLATION_IDENTITY_SCHEMA_VERSION
    ):
        raise InstallationIdentityError("unsupported installation identity schema")

    identifier = payload["installation_id"]
    if not isinstance(identifier, str):
        raise InstallationIdentityError("invalid installation identity UUID")
    try:
        parsed = UUID(identifier)
    except (ValueError, AttributeError) as exc:
        raise InstallationIdentityError("invalid installation identity UUID") from exc
    if parsed.version != 4 or str(parsed) != identifier:
        raise InstallationIdentityError("installation identity must be a canonical UUIDv4")

    mode = payload["mode"]
    if mode == "legacy_compatible":
        disposition = InstallationDisposition.CLAIMED_LEGACY
    elif mode == "opt_in":
        disposition = InstallationDisposition.CLAIMED_OPT_IN
    else:
        raise InstallationIdentityError("unknown installation identity mode")
    return InstallationInspection(disposition, identifier)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate installation identity field")
        result[key] = value
    return result
