"""Disposable offline dataset pair fixture for v11, SQLite and rollback tests."""

from __future__ import annotations

import shutil
from pathlib import Path

from _legacy_dataset import legacy_root as _root

from news_bulletin_playlist.legacy_catalog_snapshot import write_legacy_snapshot_v2
from news_bulletin_playlist.offline_dataset_inventory import inventory_offline_dataset
from news_bulletin_playlist.v11_candidate_storage import publish_v11_candidate


def _two_roots(tmp_path: Path, version: int = 10) -> tuple[Path, Path]:
    root, preview = _root(tmp_path, version)
    write_legacy_snapshot_v2(root, preview)
    publish_v11_candidate(root)
    (root / "history.sqlite3-wal").write_bytes(b"synthetic wal")
    (root / "history.sqlite3-shm").write_bytes(b"synthetic shm")
    nested = root / "custom-covers"
    nested.mkdir()
    (nested / "image.jpg").write_bytes(b"synthetic cover")
    backup = tmp_path / "independent-backup"
    shutil.copytree(root, backup)
    assert inventory_offline_dataset(root) == inventory_offline_dataset(backup)
    return root, backup
