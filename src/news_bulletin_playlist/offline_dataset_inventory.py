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
class DatasetFileEvidence:
    path: str
    size: int
    sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class OfflineDatasetInventory:
    directories: tuple[str, ...]
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


def _scan(root: Path, device: int, inode: int) -> OfflineDatasetInventory:
    directories: list[str] = []
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
            directories.append(relative)
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
                    ):
                        raise DatasetInventoryError("dataset file changed before hashing")
                    digest = hashlib.sha256()
                    while block := os.read(fd, _BLOCK_SIZE):
                        digest.update(block)
                    after = os.fstat(fd)
                    if (
                        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                        or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                    ):
                        raise DatasetInventoryError("dataset file changed while hashing")
                    name_in_root = name if relative == "." else f"{relative}/{name}"
                    files.append(
                        DatasetFileEvidence(
                            name_in_root,
                            after.st_size,
                            digest.hexdigest(),
                            stat.S_IMODE(after.st_mode),
                        )
                    )
                    total += after.st_size
                finally:
                    os.close(fd)
    except OSError as exc:
        raise DatasetInventoryError("dataset inventory could not be completed safely") from exc
    return OfflineDatasetInventory(
        tuple(sorted(directories)),
        tuple(sorted(files, key=lambda item: item.path)),
        total,
    )
