"""Explicit forward-only schema-v11 owner store (not yet wired to startup).

Reads/writes exactly one v11 authority and retains legacy managed-state.json
only as inert evidence. Atomic creation and CAS edits never fall back on a
missing or invalid v11 file. Caller integration must prevent legacy writes.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.legacy_catalog_snapshot import build_legacy_snapshot_v2
from news_bulletin_playlist.legacy_materialization import preview_legacy_materialization
from news_bulletin_playlist.managed_state import ManagedState, ManagedStateStore
from news_bulletin_playlist.v11_candidate import build_v11_candidate
from news_bulletin_playlist.v11_candidate_storage import _locked_root, _read_regular
from news_bulletin_playlist.v11_operational_codec import (
    MAX_OPERATIONAL_BYTES,
    V11OperationalError,
    V11OperationalState,
    build_v11_operational_from_candidate,
    parse_v11_operational,
    update_v11_operational,
)

V11_OPERATIONAL_FILENAME = "managed-state-v11.json"
_LEGACY_FILENAME = "managed-state.json"


class V11StoreError(ValueError):
    """Invalid v11 ownership evidence, collision or uncertain write."""


def _read_existing(root_fd: int) -> bytes | None:
    try:
        os.stat(V11_OPERATIONAL_FILENAME, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    try:
        raw = _read_regular(root_fd, V11_OPERATIONAL_FILENAME, MAX_OPERATIONAL_BYTES)
        parse_v11_operational(raw)
        return raw
    except (OSError, ValueError) as exc:
        raise V11StoreError("existing v11 state is invalid; never fall back to legacy") from exc


def _write_temporary(root_fd: int, payload: bytes) -> str:
    name = f".managed-state-v11-{uuid4().hex}.tmp"
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=root_fd,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return name
    except BaseException:
        with suppress(OSError):
            os.unlink(name, dir_fd=root_fd)
        raise


class V11OperationalStore:
    """A deliberate forward-only authority, independent of image catalog drift."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load_bytes(self) -> bytes:
        with _locked_root(self.root) as dir_fd:
            raw = _read_existing(dir_fd)
            if raw is None:
                raise V11StoreError("v11 is not initialized; no legacy fallback")
            return raw

    def load(self) -> V11OperationalState:
        return parse_v11_operational(self.load_bytes())

    def initialize_from_legacy(
        self, *, catalog: BuiltInCatalog = BUILTIN_CATALOG
    ) -> V11OperationalState:
        """One-shot opt-in legacy v6-v10 conversion. No provider or SQLite I/O.

        Never overwrites legacy records or drops a preexisting v11 document.
        """
        with _locked_root(self.root) as dir_fd:
            prior = _read_existing(dir_fd)
            if prior is not None:
                return parse_v11_operational(prior)
            try:
                original = _read_regular(dir_fd, _LEGACY_FILENAME, 12 * 1024 * 1024)
                legacy = ManagedStateStore(
                    self.root / _LEGACY_FILENAME, catalog=catalog
                ).load_readonly()
                preview = preview_legacy_materialization(catalog, legacy)
                frozen = build_legacy_snapshot_v2(preview, original)
                next_bytes = build_v11_operational_from_candidate(
                    build_v11_candidate(frozen, original)
                )
                if _read_regular(dir_fd, _LEGACY_FILENAME, 12 * 1024 * 1024) != original:
                    raise V11StoreError("legacy state changed during forward migration")
                temporary = _write_temporary(dir_fd, next_bytes)
                try:
                    # Publish only if the new authority does not already exist.
                    os.link(
                        temporary,
                        V11_OPERATIONAL_FILENAME,
                        src_dir_fd=dir_fd,
                        dst_dir_fd=dir_fd,
                        follow_symlinks=False,
                    )
                    os.fsync(dir_fd)
                finally:
                    os.unlink(temporary, dir_fd=dir_fd)
                    os.fsync(dir_fd)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise V11StoreError(
                    "forward-only v11 initialization failed; inspect both state files"
                ) from exc
            return parse_v11_operational(next_bytes)

    def save(self, state: ManagedState, *, expected_bytes: bytes) -> bytes:
        """Atomically replace validated v11 overlay with an exact-byte CAS.

        No writes are allowed without the caller's unmodified prior v11 bytes.
        """
        with _locked_root(self.root) as dir_fd:
            current = _read_existing(dir_fd)
            if current is None:
                raise V11StoreError("v11 is missing; cannot fall back to legacy")
            if not isinstance(expected_bytes, bytes) or current != expected_bytes:
                raise V11StoreError("stale v11 edit; reload before saving")
            try:
                next_bytes = update_v11_operational(current, state)
            except V11OperationalError as exc:
                raise V11StoreError("invalid v11 owner edit") from exc
            if next_bytes == current:
                return current
            try:
                temporary = _write_temporary(dir_fd, next_bytes)
                try:
                    # All cooperative writers take the same directory lock.
                    if _read_existing(dir_fd) != current:
                        raise V11StoreError("v11 changed before atomic save")
                    os.replace(
                        temporary,
                        V11_OPERATIONAL_FILENAME,
                        src_dir_fd=dir_fd,
                        dst_dir_fd=dir_fd,
                    )
                    os.fsync(dir_fd)
                finally:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=dir_fd)
            except OSError as exc:
                raise V11StoreError("v11 write durability uncertain; inspect before retry") from exc
            return next_bytes
