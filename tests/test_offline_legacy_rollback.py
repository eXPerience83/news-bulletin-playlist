"""Read-only restored legacy-root acceptance without network or container writes."""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
from pathlib import Path

import pytest
from test_v11_shadow_staging import _two_roots

from news_bulletin_playlist.offline_dataset_inventory import inventory_offline_dataset
from news_bulletin_playlist.offline_legacy_rollback import (
    OfflineLegacyRollbackError,
    rehearse_offline_legacy_rollback,
)
from news_bulletin_playlist.offline_sqlite_backup import RUNTIME_SQLITE_FILENAME
from news_bulletin_playlist.v11_shadow_staging import stage_v11_shadow_state


def _three_roots(tmp_path: Path, schema: int = 10) -> tuple[Path, Path, Path]:
    root, backup = _two_roots(tmp_path, schema)
    with sqlite3.connect(root / RUNTIME_SQLITE_FILENAME) as connection:
        connection.execute("CREATE TABLE accepted_history (id INTEGER PRIMARY KEY, item TEXT)")
        connection.execute("INSERT INTO accepted_history VALUES (1, 'retained')")
    shutil.copy2(root / RUNTIME_SQLITE_FILENAME, backup / RUNTIME_SQLITE_FILENAME)
    assert inventory_offline_dataset(root) == inventory_offline_dataset(backup)
    stage_v11_shadow_state(root, backup, operator_asserts_offline=True)
    restored = tmp_path / "restored-legacy"
    shutil.copytree(backup, restored)
    assert inventory_offline_dataset(restored) == inventory_offline_dataset(backup)
    return root, backup, restored


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_restored_legacy_runtime_and_destinations_survive_without_writes(
    tmp_path: Path, schema: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, backup, restored = _three_roots(tmp_path, schema)
    baseline = tuple(inventory_offline_dataset(path) for path in (root, backup, restored))

    def no_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError("provider activity forbidden during isolated restore")

    monkeypatch.setattr(socket, "socket", no_socket)
    result = rehearse_offline_legacy_rollback(
        root, backup, restored, operator_asserts_offline=True
    )
    assert result.managed_schema_version == schema
    assert result.destination_count == 4
    assert result.sqlite_table_count == 1
    assert result.proves_old_image_compatibility is False
    assert result.permits_cutover is False
    assert tuple(inventory_offline_dataset(path) for path in (root, backup, restored)) == (
        baseline
    )


def test_missing_offline_assertion_rejected_without_mutation(tmp_path: Path) -> None:
    root, backup, restored = _three_roots(tmp_path)
    before = tuple(inventory_offline_dataset(x) for x in (root, backup, restored))
    with pytest.raises(OfflineLegacyRollbackError, match="assertion"):
        rehearse_offline_legacy_rollback(root, backup, restored)
    assert tuple(inventory_offline_dataset(x) for x in (root, backup, restored)) == before


@pytest.mark.parametrize("fault", ["cover", "oauth", "sqlite", "state", "journal"])
def test_restored_dataset_tampering_blocks_legacy_rehearsal(
    tmp_path: Path, fault: str
) -> None:
    root, backup, restored = _three_roots(tmp_path)
    target = {
        "cover": restored / "custom-covers" / "image.jpg",
        "oauth": restored / "oauth-private",
        "sqlite": restored / RUNTIME_SQLITE_FILENAME,
        "state": restored / "managed-state.json",
        "journal": restored / "pending-post.json",
    }[fault]
    target.write_bytes(b"corrupted after restore")
    before = inventory_offline_dataset(backup)
    with pytest.raises(OfflineLegacyRollbackError):
        rehearse_offline_legacy_rollback(root, backup, restored, operator_asserts_offline=True)
    assert inventory_offline_dataset(backup) == before


def test_missing_shadow_or_new_state_cannot_be_treated_as_legacy_restore(
    tmp_path: Path,
) -> None:
    root, backup, restored = _three_roots(tmp_path)
    (root / "managed-state-v11.shadow.json").unlink()
    with pytest.raises(OfflineLegacyRollbackError):
        rehearse_offline_legacy_rollback(root, backup, restored, operator_asserts_offline=True)


@pytest.mark.parametrize("invalid", ["same", "nested", "hardlink", "symlink"])
def test_nonindependent_restore_rejected(tmp_path: Path, invalid: str) -> None:
    root, backup, restored = _three_roots(tmp_path)
    if invalid == "same":
        restored = backup
    elif invalid == "nested":
        restored = backup / "inside"
        restored.mkdir()
    elif invalid == "hardlink":
        target = restored / RUNTIME_SQLITE_FILENAME
        target.unlink()
        os.link(backup / RUNTIME_SQLITE_FILENAME, target)
    else:
        link = tmp_path / "linked"
        link.symlink_to(restored, target_is_directory=True)
        restored = link
    with pytest.raises(OfflineLegacyRollbackError):
        rehearse_offline_legacy_rollback(root, backup, restored, operator_asserts_offline=True)
