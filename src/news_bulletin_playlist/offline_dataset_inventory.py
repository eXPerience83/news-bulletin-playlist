"""Read-only offline /data inventory; NOT a consistent snapshot or backup.

Only a stopped application plus an independently verified backup can make
the file set suitable for a future migration transaction. Scanning twice
detects many races but cannot prove that the application was quiescent.
Do not call this on production /data while its application is running.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

_BLOCK_SIZE = 1024 * 1024
_MAX_FILES = 100_000


class DatasetInventoryError(ValueError):
    """Unsafe file types or unstable evidence: never authorize cutover."""


@dataclass(frozen=True, slots=True)
class DatasetDirectoryEvidence:
    path: str
    mode: int
    uid: int
    gid: int
    xattr_digests: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class DatasetFileEvidence:
    path: str
    size: int
    sha256: str
    mode: int
    uid: int
    gid: int
    nlink: int
    xattr_digests: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class OfflineDatasetInventory:
    directories: tuple[DatasetDirectoryEvidence, ...]
    files: tuple[DatasetFileEvidence, ...]
    total_bytes: int

    @property
    def authorizes_migration(self) -> bool:
        """A matching inventory alone never proves quiescence or rollback."""
        return False


def inventory_offline_dataset(root: Path) -> OfflineDatasetInventory:
    """Inspect every regular file twice, fail closed on changes or unsafe paths.

    Enforces one filesystem, no links, no FIFOs/sockets/devices, no hidden
    omissions, stable inode/size/mtime during hashing, and two matching
    top-to-bottom inventories. Never writes to the directory.
    """
    try:
        info = root.lstat()
    except OSError as exc:
        raise DatasetInventoryError("offline dataset root is not accessible") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise DatasetInventoryError("offline dataset root must be a real directory")
    first = _scan(root, info.st_dev, info.st_ino)
    second = _scan(root, info.st_dev, info.st_ino)
    if first != second:
        raise DatasetInventoryError("dataset changed between inventory passes")
    return first


def _xattrs(fd: int) -> tuple[tuple[str, str], ...]:
    """Hash extended attributes; do not expose their possibly private values."""
    try:
        names = sorted(os.listxattr(fd))
        if len(names) > 256:
            raise DatasetInventoryError("too many dataset extended attributes")
        return tuple((name, hashlib.sha256(os.getxattr(fd, name)).hexdigest()) for name in names)
    except OSError as exc:
        raise DatasetInventoryError("cannot read dataset extended attributes safely") from exc


def _scan(root: Path, device: int, inode: int) -> OfflineDatasetInventory:
    directories: list[DatasetDirectoryEvidence] = []
    files: list[DatasetFileEvidence] = []
    total = 0
    try:
        walker = os.fwalk(root, topdown=True, follow_symlinks=False)
        for path, subdirs, filenames, dirfd in walker:
            relative = Path(path).relative_to(root).as_posix()
            here = os.fstat(dirfd)
            by_path = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISDIR(here.st_mode)
                or here.st_dev != device
                or (here.st_dev, here.st_ino) != (by_path.st_dev, by_path.st_ino)
                or (relative == "." and here.st_ino != inode)
            ):
                raise DatasetInventoryError("dataset directory identity changed")
            attrs = _xattrs(dirfd)
            after_dir = os.fstat(dirfd)
            if (here.st_mode, here.st_uid, here.st_gid, here.st_ctime_ns, here.st_mtime_ns) != (
                after_dir.st_mode,
                after_dir.st_uid,
                after_dir.st_gid,
                after_dir.st_ctime_ns,
                after_dir.st_mtime_ns,
            ):
                raise DatasetInventoryError("dataset directory metadata changed")
            directories.append(
                DatasetDirectoryEvidence(
                    relative,
                    stat.S_IMODE(after_dir.st_mode),
                    after_dir.st_uid,
                    after_dir.st_gid,
                    attrs,
                )
            )
            for name in sorted(subdirs):
                child = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                if not stat.S_ISDIR(child.st_mode) or child.st_dev != device:
                    raise DatasetInventoryError("unsafe link or mounted subdirectory")
            subdirs.sort()
            for name in sorted(filenames):
                if len(files) >= _MAX_FILES:
                    raise DatasetInventoryError("dataset inventory file-count limit exceeded")
                metadata = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_dev != device:
                    raise DatasetInventoryError("unsafe link or nonregular dataset file")
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=dirfd,
                )
                try:
                    before = os.fstat(fd)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino)
                        or before.st_size != metadata.st_size
                        or (
                            before.st_mode,
                            before.st_uid,
                            before.st_gid,
                            before.st_nlink,
                            before.st_ctime_ns,
                        )
                        != (
                            metadata.st_mode,
                            metadata.st_uid,
                            metadata.st_gid,
                            metadata.st_nlink,
                            metadata.st_ctime_ns,
                        )
                    ):
                        raise DatasetInventoryError("dataset file changed before hashing")
                    digest = hashlib.sha256()
                    while block := os.read(fd, _BLOCK_SIZE):
                        digest.update(block)
                    attrs = _xattrs(fd)
                    after = os.fstat(fd)
                    if (
                        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                        or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                        or before.st_ctime_ns != after.st_ctime_ns
                        or before.st_mode != after.st_mode
                        or before.st_uid != after.st_uid
                        or before.st_gid != after.st_gid
                        or before.st_nlink != after.st_nlink
                    ):
                        raise DatasetInventoryError("dataset file changed while hashing")
                    name_in_root = name if relative == "." else f"{relative}/{name}"
                    files.append(
                        DatasetFileEvidence(
                            name_in_root,
                            after.st_size,
                            digest.hexdigest(),
                            stat.S_IMODE(after.st_mode),
                            after.st_uid,
                            after.st_gid,
                            after.st_nlink,
                            attrs,
                        )
                    )
                    total += after.st_size
                finally:
                    os.close(fd)
    except OSError as exc:
        raise DatasetInventoryError("dataset inventory could not be completed safely") from exc
    return OfflineDatasetInventory(
        tuple(sorted(directories, key=lambda item: item.path)),
        tuple(sorted(files, key=lambda item: item.path)),
        total,
    )
