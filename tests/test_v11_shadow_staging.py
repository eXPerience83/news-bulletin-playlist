"""Shadow-only offline staging; never an actual managed-state cutover."""

from __future__ import annotations

import os
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_legacy_catalog_snapshot import _root

from news_bulletin_playlist.legacy_catalog_snapshot import write_legacy_snapshot_v2
from news_bulletin_playlist.offline_dataset_inventory import inventory_offline_dataset
from news_bulletin_playlist.v11_candidate_storage import publish_v11_candidate
from news_bulletin_playlist.v11_shadow_staging import (
    V11_SHADOW_FILENAME,
    V11ShadowStageError,
    inspect_v11_shadow_state,
    stage_v11_shadow_state,
)


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


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("version", [6, 7, 8, 9, 10])
def test_offline_stage_preserves_every_original_byte_and_restarts(
    tmp_path: Path, version: int
) -> None:
    root, backup = _two_roots(tmp_path, version)
    original = _files(root)
    saved = _files(backup)
    assert stage_v11_shadow_state(root, backup, operator_asserts_offline=True)
    after = _files(root)
    assert {k: v for k, v in after.items() if k != V11_SHADOW_FILENAME} == original
    assert _files(backup) == saved
    assert stat.S_IMODE((root / V11_SHADOW_FILENAME).stat().st_mode) == 0o600
    assert not any(".tmp" in name for name in after)
    second = stage_v11_shadow_state(root, backup, operator_asserts_offline=True)
    assert second == after[V11_SHADOW_FILENAME]
    inspected = inspect_v11_shadow_state(root, backup)
    assert inspected.shadow_bytes == second
    assert len(inspected.state.managed_overlay.playlists) == 4
    assert inspected.state.is_live_migration_authorized is False
    assert inspected.permits_cutover is False
    assert _files(root) == after


def test_explicit_ack_is_required_and_does_not_assert_quiescence(tmp_path: Path) -> None:
    root, backup = _two_roots(tmp_path)
    before = _files(root)
    with pytest.raises(V11ShadowStageError, match="operator assertion"):
        stage_v11_shadow_state(root, backup)
    assert _files(root) == before


@pytest.mark.parametrize("kind", ["same", "nested", "hardlink", "changed"])
def test_untrustworthy_or_nonindependent_backup_is_rejected(tmp_path: Path, kind: str) -> None:
    root, backup = _two_roots(tmp_path)
    if kind == "same":
        backup = root
    elif kind == "nested":
        backup = root / "subdirectory"
        backup.mkdir()
    elif kind == "hardlink":
        target = backup / "history.sqlite3"
        target.unlink()
        os.link(root / "history.sqlite3", target)
    else:
        (backup / "history.sqlite3").write_bytes(b"changed SQLite backup")
    with pytest.raises(V11ShadowStageError):
        stage_v11_shadow_state(root, backup, operator_asserts_offline=True)
    assert not (root / V11_SHADOW_FILENAME).exists()


def test_live_state_change_or_different_backup_refuses_staging(tmp_path: Path) -> None:
    root, backup = _two_roots(tmp_path)
    (root / "managed-state.json").write_bytes(b"simulated conflicting live state")
    with pytest.raises(V11ShadowStageError):
        stage_v11_shadow_state(root, backup, operator_asserts_offline=True)
    assert not (root / V11_SHADOW_FILENAME).exists()


def test_existing_shadow_cannot_be_overwritten_or_replaced(tmp_path: Path) -> None:
    root, backup = _two_roots(tmp_path)
    marker = root / V11_SHADOW_FILENAME
    marker.write_bytes(b"conflicting v11 shadow")
    with pytest.raises(V11ShadowStageError, match="conflicts"):
        stage_v11_shadow_state(root, backup, operator_asserts_offline=True)
    assert marker.read_bytes() == b"conflicting v11 shadow"
    marker.unlink()
    marker.symlink_to(root / "managed-state.json")
    with pytest.raises(V11ShadowStageError):
        stage_v11_shadow_state(root, backup, operator_asserts_offline=True)
    assert marker.is_symlink()


def test_cooperative_stage_calls_are_idempotent(tmp_path: Path) -> None:
    root, backup = _two_roots(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = tuple(
            pool.map(
                lambda _: stage_v11_shadow_state(root, backup, operator_asserts_offline=True),
                range(2),
            )
        )
    assert first == second
    assert inspect_v11_shadow_state(root, backup).shadow_bytes == first


def test_failure_before_link_does_not_change_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, backup = _two_roots(tmp_path)
    before = _files(root)

    def no_link(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic failure before exclusive link")

    monkeypatch.setattr(os, "link", no_link)
    with pytest.raises(V11ShadowStageError, match="publication uncertain"):
        stage_v11_shadow_state(root, backup, operator_asserts_offline=True)
    assert _files(root) == before


def test_post_link_directory_fsync_failure_leaves_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, backup = _two_roots(tmp_path)
    original_fsync = os.fsync
    injected = False

    def fail_directory(fd: int) -> None:
        nonlocal injected
        if not injected and stat.S_ISDIR(os.fstat(fd).st_mode):
            injected = True
            raise OSError("simulated post-link durability uncertainty")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(V11ShadowStageError, match="publication uncertain"):
        stage_v11_shadow_state(root, backup, operator_asserts_offline=True)
    assert (root / V11_SHADOW_FILENAME).is_file()
    assert any(".tmp" in item.name for item in root.iterdir())
    with pytest.raises(V11ShadowStageError):
        inspect_v11_shadow_state(root, backup)
    # Manual synthetic cleanup is NOT automatic recovery of a production volume.
    for item in root.iterdir():
        if item.name.endswith(".tmp"):
            item.unlink()
    assert inspect_v11_shadow_state(root, backup).permits_cutover is False


def test_verify_fails_after_backup_or_shadow_mutation_without_writes(tmp_path: Path) -> None:
    root, backup = _two_roots(tmp_path)
    stage_v11_shadow_state(root, backup, operator_asserts_offline=True)
    before = _files(root)
    (backup / "custom-covers" / "image.jpg").write_bytes(b"tampered offline backup")
    with pytest.raises(V11ShadowStageError):
        inspect_v11_shadow_state(root, backup)
    assert _files(root) == before


def test_offline_shadow_rejects_backup_with_identical_bytes_but_different_mode(
    tmp_path: Path,
) -> None:
    root, backup = _two_roots(tmp_path)
    copied_cover = backup / "custom-covers" / "image.jpg"
    copied_cover.chmod(copied_cover.stat().st_mode ^ 0o100)
    assert copied_cover.read_bytes() == (root / "custom-covers" / "image.jpg").read_bytes()
    with pytest.raises(V11ShadowStageError, match="differs"):
        stage_v11_shadow_state(root, backup, operator_asserts_offline=True)
    assert not (root / V11_SHADOW_FILENAME).exists()
