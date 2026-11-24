"""Normalize newly created private persistence objects on ACL-inheriting filesystems.

Only call this for a descriptor just created by this process, never to repair
an arbitrary pre-existing object. Do not infer NFSv4 named ACE privacy from
POSIX mode bits alone; the operator must provide a trusted data root.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def ensure_private_created(fd: int, *, directory: bool = False) -> None:
    """Remove inherited group/other permissions before writing or publication."""
    metadata = os.fstat(fd)
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    required = 0o700 if directory else 0o600
    if not kind(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise PermissionError("New private persistence object has unexpected type or owner")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077 or mode & required != required:
        os.fchmod(fd, required)
        metadata = os.fstat(fd)
        mode = stat.S_IMODE(metadata.st_mode)
    # TrueNAS NFSv4 chmod(0600) can preserve the owner's execute bit.
    if (
        not kind(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or mode & 0o077
        or mode & required != required
    ):
        raise PermissionError("Cannot establish private persistence permissions")


def open_private_directory(path: Path, *, create: bool) -> int:
    """Open a private directory; normalize only a directory created here."""
    created = False
    if create:
        try:
            path.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if created:
            ensure_private_created(fd, directory=True)
        mode = os.fstat(fd).st_mode
        if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o077:
            raise PermissionError("Private directory permissions are unsafe")
        return fd
    except BaseException:
        os.close(fd)
        raise
