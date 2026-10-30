"""Explicit, isolated identity bootstrap primitive for genuinely empty Linux data roots.

This is intentionally NOT wired into engine/runtime startup. Until #552's
versioned legacy migration and opt-in catalog split are complete, callers must
not use this to change active installation behavior or create Spotify objects.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path
from uuid import uuid4

from news_bulletin_playlist.installation_identity import (
    INSTALLATION_IDENTITY_FILENAME,
    INSTALLATION_IDENTITY_SCHEMA_VERSION,
    InstallationDisposition,
    InstallationIdentityError,
    InstallationInspection,
    inspect_installation_root,
)


def claim_pristine_installation_root(data_dir: Path) -> InstallationInspection:
    """Exclusively claim an existing, *empty* real directory as opt-in.

    Linux directory flock coordinates competing bootstrappers without adding a
    lockfile that would make a fresh root appear legacy. A temporary file is
    fully written and fsynced before an exclusive hard link publishes the final
    marker; the directory is fsynced after publication and cleanup.

    Never create the directory, mark a legacy root, follow a root symlink,
    overwrite existing evidence or repair an ambiguous state automatically.
    If publication was attempted but its durability is uncertain, fail closed
    and let an operator inspect the marker before any retry.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(data_dir, flags)
    except OSError as exc:
        raise InstallationIdentityError(
            "installation root must be an existing real directory"
        ) from exc

    try:
        try:
            fcntl.flock(root_fd, fcntl.LOCK_EX)
            before = os.fstat(root_fd)
            current = data_dir.lstat()
            if (
                not stat.S_ISDIR(before.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise InstallationIdentityError("installation root changed during bootstrap")

            inspection = inspect_installation_root(data_dir)
            if inspection.disposition is not InstallationDisposition.EMPTY_UNCLAIMED:
                return inspection
            # Recheck the actual locked directory, not just an earlier path scan.
            if os.listdir(root_fd):
                return InstallationInspection(InstallationDisposition.LEGACY_COMPATIBLE)

            installation_id = str(uuid4())
            payload = (
                json.dumps(
                    {
                        "schema_version": INSTALLATION_IDENTITY_SCHEMA_VERSION,
                        "installation_id": installation_id,
                        "mode": "opt_in",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            temporary_name = f".installation-identity-{uuid4().hex}.tmp"
            linked = False
            created = False
            try:
                temp_fd = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=root_fd,
                )
                created = True
                try:
                    written = 0
                    while written < len(payload):
                        n = os.write(temp_fd, payload[written:])
                        if n <= 0:
                            raise OSError("short installation identity write")
                        written += n
                    os.fsync(temp_fd)
                finally:
                    os.close(temp_fd)

                # A foreign file appearing after the first scan must prevent
                # opt-in; its origin is unknown, even if named like a temp file.
                if set(os.listdir(root_fd)) != {temporary_name}:
                    raise InstallationIdentityError("data appeared during empty-root bootstrap")

                # link() is atomic and exclusive; unlike replace(), it cannot
                # overwrite another process's or operator's identity marker.
                os.link(
                    temporary_name,
                    INSTALLATION_IDENTITY_FILENAME,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                    follow_symlinks=False,
                )
                linked = True
                os.fsync(root_fd)
                os.unlink(temporary_name, dir_fd=root_fd)
                os.fsync(root_fd)
                return InstallationInspection(
                    InstallationDisposition.CLAIMED_OPT_IN, installation_id
                )
            finally:
                if created and not linked:
                    try:
                        os.unlink(temporary_name, dir_fd=root_fd)
                        os.fsync(root_fd)
                    except OSError:
                        # A stranded temp file is conservatively legacy evidence,
                        # never automatic permission to retry opt-in activation.
                        pass
        except OSError as exc:
            raise InstallationIdentityError(
                "installation identity bootstrap failed; inspect state before retry"
            ) from exc
    finally:
        os.close(root_fd)
