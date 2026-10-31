"""Explicit, isolated Linux publication and recovery for a v11 candidate.

NEVER used by startup or by ManagedStateStore. Does not replace legacy state
or any OAuth, Spotify, SQLite, journal, artwork or playlist destination data.
Directory flock coordinates only other cooperative writers, not the running
application's ordinary configuration writes; production use is NOT authorized.
"""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from news_bulletin_playlist.legacy_catalog_snapshot import (
    FROZEN_LEGACY_SNAPSHOT_V2_FILENAME,
    MAX_FROZEN_SNAPSHOT_BYTES,
    MAX_MANAGED_STATE_BYTES,
)
from news_bulletin_playlist.v11_candidate import (
    MAX_CANDIDATE_BYTES,
    V11Candidate,
    V11CandidateError,
    build_v11_candidate,
    parse_v11_candidate,
)

V11_CANDIDATE_FILENAME = "managed-state-v11.candidate.json"
_LEGACY_FILENAME = "managed-state.json"


@contextmanager
def _locked_root(root: Path) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(root, flags)
    except OSError as exc:
        raise V11CandidateError("expected existing, non-symlink legacy root") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            before = os.fstat(fd)
            current = root.lstat()
            if (
                not stat.S_ISDIR(current.st_mode)
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise V11CandidateError("legacy root changed while acquiring lock")
        except OSError as exc:
            raise V11CandidateError("could not lock or verify legacy root") from exc
        yield fd
    finally:
        os.close(fd)


def _read_regular(dir_fd: int, filename: str, limit: int) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(filename, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise V11CandidateError(f"missing or unsafe candidate prerequisite: {filename}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise V11CandidateError("candidate prerequisite is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read(limit + 1)
        if not 0 < len(payload) <= limit:
            raise V11CandidateError("invalid candidate prerequisite size")
        return payload
    finally:
        os.close(fd)


def _read_existing_candidate(dir_fd: int) -> bytes | None:
    try:
        os.stat(V11_CANDIDATE_FILENAME, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _read_regular(dir_fd, V11_CANDIDATE_FILENAME, MAX_CANDIDATE_BYTES)


def _require_match(dir_fd: int, original: bytes, frozen: bytes) -> None:
    if _read_regular(dir_fd, _LEGACY_FILENAME, MAX_MANAGED_STATE_BYTES) != original:
        raise V11CandidateError("legacy managed state changed; candidate publication denied")
    if _read_regular(dir_fd, FROZEN_LEGACY_SNAPSHOT_V2_FILENAME, MAX_FROZEN_SNAPSHOT_BYTES) != (
        frozen
    ):
        raise V11CandidateError("frozen v2 snapshot changed; candidate publication denied")


def publish_v11_candidate(root: Path) -> bytes:
    """Publish only to a new, exclusive 0600 file; never modify existing state.

    Require a previously published frozen-v2 sidecar and matching original
    legacy managed state. A failed directory fsync after linking is ambiguous;
    leave visible marker and staging evidence for manual inspection instead
    of deleting or silently retrying a different candidate.
    """
    with _locked_root(root) as dir_fd:
        original = _read_regular(dir_fd, _LEGACY_FILENAME, MAX_MANAGED_STATE_BYTES)
        frozen = _read_regular(
            dir_fd, FROZEN_LEGACY_SNAPSHOT_V2_FILENAME, MAX_FROZEN_SNAPSHOT_BYTES
        )
        expected = build_v11_candidate(frozen, original)
        previous = _read_existing_candidate(dir_fd)
        if previous is not None:
            if previous != expected:
                raise V11CandidateError("existing candidate differs; no overwrite")
            return previous

        temporary = f".managed-state-v11-candidate-{uuid4().hex}.tmp"
        linked = False
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(expected)
                stream.flush()
                os.fsync(stream.fileno())
            _require_match(dir_fd, original, frozen)
            os.link(
                temporary,
                V11_CANDIDATE_FILENAME,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
                follow_symlinks=False,
            )
            linked = True
            os.fsync(dir_fd)
            os.unlink(temporary, dir_fd=dir_fd)
            os.fsync(dir_fd)
            return expected
        except OSError as exc:
            raise V11CandidateError(
                "candidate publication uncertain; inspect on-disk evidence before retry"
            ) from exc
        finally:
            if not linked:
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=dir_fd)
                    os.fsync(dir_fd)


def read_v11_candidate(root: Path, *, require_legacy_unchanged: bool = True) -> V11Candidate:
    """Read-only recovery inspection. Never restore or overwrite managed-state.

    When the future live schema differs, callers may request recovery bytes
    without requiring that the old managed-state still matches. The candidate
    and frozen-v2 sidecar must still match *exactly*; no restoration is made.
    """
    with _locked_root(root) as dir_fd:
        payload = _read_regular(dir_fd, V11_CANDIDATE_FILENAME, MAX_CANDIDATE_BYTES)
        result = parse_v11_candidate(payload)
        frozen = _read_regular(
            dir_fd, FROZEN_LEGACY_SNAPSHOT_V2_FILENAME, MAX_FROZEN_SNAPSHOT_BYTES
        )
        if frozen != result.frozen_catalog_v2_bytes:
            raise V11CandidateError("candidate and frozen-v2 evidence differ")
        if require_legacy_unchanged and (
            _read_regular(dir_fd, _LEGACY_FILENAME, MAX_MANAGED_STATE_BYTES)
            != result.original_managed_state_bytes
        ):
            raise V11CandidateError("original managed state has changed")
        return result
