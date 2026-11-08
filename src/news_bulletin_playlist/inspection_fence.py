"""Durable operator inspection gate for a forward-only v11 cutover.

A one-time explicit inspection request is persisted before the legacy conversion.
The gate remains held across crashes, restarts and environment changes, and is
only released by an explicit, exact-state-hash CLI acknowledgement. No remote
Spotify or SQLite interaction is performed by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from news_bulletin_playlist.v11_candidate_storage import _locked_root, _read_regular
from news_bulletin_playlist.v11_operational_codec import (
    MAX_OPERATIONAL_BYTES,
    parse_v11_operational,
)
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME

INSPECTION_ENV = "NEWS_PLAYLIST_STARTUP_INSPECTION"
INSPECTION_FILENAME = ".spotify-reconciliation-inspection.json"
RELEASE_CONFIRMATION = "I-REVIEWED-V11-ENABLE-SPOTIFY-WRITES"
_FORMAT = "news-bulletin-playlist.spotify-inspection"
_MAX_MARKER_BYTES = 512
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class InspectionGateError(ValueError):
    """Invalid inspection evidence always blocks a normal startup or release."""


@dataclass(frozen=True, slots=True)
class InspectionGateStatus:
    state: str
    state_sha256: str
    original_release_sha256: str | None = None

    @property
    def held(self) -> bool:
        return self.state == "held"


def inspection_requested(environ: Mapping[str, str]) -> bool:
    """Accept an explicit 1 (inspect) or 0/unset (no new request), nothing else."""
    value = environ.get(INSPECTION_ENV)
    if value is None or value == "0":
        return False
    if value == "1":
        return True
    raise InspectionGateError(f"{INSPECTION_ENV} must be exactly 0 or 1")


def _canonical(state: str, sha256: str | None = None) -> bytes:
    data: dict[str, object] = {"format": _FORMAT, "version": 1, "state": state}
    if state == "released":
        if sha256 is None or _SHA256.fullmatch(sha256) is None:
            raise InspectionGateError("Release marker has an invalid v11 SHA-256")
        data["release_sha256"] = sha256
    elif state != "held" or sha256 is not None:
        raise InspectionGateError("Invalid inspection gate state")
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _read(fd: int) -> tuple[str, str | None] | None:
    try:
        opened = os.open(
            INSPECTION_FILENAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InspectionGateError("Inspection gate cannot be opened safely") from exc
    try:
        metadata = os.fstat(opened)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise InspectionGateError("Inspection gate must be a private regular file")
        with os.fdopen(opened, "rb", closefd=False) as stream:
            content = stream.read(_MAX_MARKER_BYTES + 1)
    except OSError as exc:
        raise InspectionGateError("Inspection gate cannot be read") from exc
    finally:
        os.close(opened)
    if not 0 < len(content) <= _MAX_MARKER_BYTES:
        raise InspectionGateError("Inspection gate has invalid size")
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InspectionGateError("Inspection gate has invalid JSON") from exc
    if not isinstance(document, dict):
        raise InspectionGateError("Inspection gate has invalid content")
    if document.get("format") != _FORMAT or type(document.get("version")) is not int:
        raise InspectionGateError("Inspection gate has invalid format")
    if document["version"] != 1 or document.get("state") not in {"held", "released"}:
        raise InspectionGateError("Inspection gate has unsupported state")
    state = document["state"]
    release_sha = document.get("release_sha256")
    expected_keys = (
        {"format", "version", "state"}
        if state == "held"
        else {"format", "version", "state", "release_sha256"}
    )
    if set(document) != expected_keys or content != _canonical(state, release_sha):
        raise InspectionGateError("Inspection gate is not canonical")
    return state, release_sha


def _write(fd: int, state: str, release_sha256: str | None = None) -> None:
    """Private atomic rewrite protected by the same root lock as the v11 store."""
    content = _canonical(state, release_sha256)
    temporary = ".spotify-inspection-" + uuid.uuid4().hex + ".tmp"
    try:
        opened = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        with os.fdopen(opened, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, INSPECTION_FILENAME, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    except OSError as exc:
        raise InspectionGateError(
            "Inspection gate publication is uncertain; inspect before retry"
        ) from exc
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def startup_gate(data_dir: Path, *, requested: bool) -> bool:
    """Return whether provider reconciliation is held this process lifetime.

    This runs before v11 legacy conversion and before scheduler initialization.
    Only pristine roots are initialized fresh first, to avoid misclassifying
    an inspection marker as a pre-existing installation identity.
    """
    try:
        with _locked_root(data_dir) as fd:
            existing = _read(fd)
            if requested:
                if existing is None or existing[0] != "held":
                    _write(fd, "held")
                return True
            return existing is not None and existing[0] == "held"
    except InspectionGateError:
        raise
    except (OSError, ValueError) as exc:
        raise InspectionGateError("Cannot safely establish Spotify inspection gate") from exc


def inspect_gate(data_dir: Path) -> InspectionGateStatus:
    """Read-only status and hash: the operator must inspect this exact v11 state."""
    try:
        with _locked_root(data_dir) as fd:
            existing = _read(fd)
            raw = _read_regular(fd, V11_OPERATIONAL_FILENAME, MAX_OPERATIONAL_BYTES)
            parse_v11_operational(raw)
        return InspectionGateStatus(
            state="not_requested" if existing is None else existing[0],
            state_sha256=hashlib.sha256(raw).hexdigest(),
            original_release_sha256=None if existing is None else existing[1],
        )
    except InspectionGateError:
        raise
    except (OSError, ValueError) as exc:
        raise InspectionGateError("Cannot verify the current owned v11 state") from exc


def release_gate(
    data_dir: Path,
    *,
    expected_sha256: str,
    confirm: str,
) -> InspectionGateStatus:
    """Explicit release; the running process stays held until an ordinary restart."""
    if confirm != RELEASE_CONFIRMATION:
        raise InspectionGateError("Explicit reconciliation release confirmation is required")
    if _SHA256.fullmatch(expected_sha256) is None:
        raise InspectionGateError("Provide the full lowercase v11 SHA-256 from inspection-status")
    try:
        with _locked_root(data_dir) as fd:
            if _read(fd) != ("held", None):
                raise InspectionGateError("No held inspection gate can be released")
            raw = _read_regular(fd, V11_OPERATIONAL_FILENAME, MAX_OPERATIONAL_BYTES)
            parse_v11_operational(raw)
            observed = hashlib.sha256(raw).hexdigest()
            if observed != expected_sha256:
                raise InspectionGateError("v11 state changed; re-inspect before releasing Spotify")
            _write(fd, "released", observed)
            if _read(fd) != ("released", observed):
                raise InspectionGateError("Reconciliation release durability is uncertain")
        return InspectionGateStatus(
            state="released",
            state_sha256=observed,
            original_release_sha256=observed,
        )
    except InspectionGateError:
        raise
    except (OSError, ValueError) as exc:
        raise InspectionGateError("Cannot safely release Spotify inspection gate") from exc
