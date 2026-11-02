"""Byte-for-byte recursive snapshots for filesystem side-effect assertions in tests."""

from __future__ import annotations

from pathlib import Path


def snapshot_files(root: Path) -> dict[str, bytes]:
    """Record relative file paths and their exact bytes without changing the tree."""
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
