"""Isolated candidate publication: old data untouched and failures fail closed."""

from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_legacy_catalog_snapshot import _root

from news_bulletin_playlist.legacy_catalog_snapshot import write_legacy_snapshot_v2
from news_bulletin_playlist.v11_candidate import V11CandidateError, parse_v11_candidate
from news_bulletin_playlist.v11_candidate_storage import (
    V11_CANDIDATE_FILENAME,
    publish_v11_candidate,
    read_v11_candidate,
)


def _prepared(tmp_path: Path, schema: int = 10) -> Path:
    root, preview = _root(tmp_path, schema)
    write_legacy_snapshot_v2(root, preview)
    return root


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_candidate_publish_read_restart_and_idempotence(tmp_path: Path, schema: int) -> None:
    root = _prepared(tmp_path, schema)
    before = _files(root)
    raw = before["managed-state.json"]
    published = publish_v11_candidate(root)
    assert parse_v11_candidate(published).original_managed_state_bytes == raw
    assert read_v11_candidate(root).original_managed_state_bytes == raw
    assert publish_v11_candidate(root) == published
    after = _files(root)
    assert after[V11_CANDIDATE_FILENAME] == published
    assert {k: v for k, v in after.items() if k != V11_CANDIDATE_FILENAME} == before
    assert stat.S_IMODE((root / V11_CANDIDATE_FILENAME).stat().st_mode) == 0o600
    assert not any(".tmp" in name for name in after)
    assert read_v11_candidate(root).projection.source_order_verified is True


def test_two_cooperating_publishers_share_one_candidate(tmp_path: Path) -> None:
    root = _prepared(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = tuple(pool.map(publish_v11_candidate, (root, root)))
    assert first == second
    assert read_v11_candidate(root).original_managed_state_bytes == (
        root / "managed-state.json"
    ).read_bytes()
    assert len([p for p in root.iterdir() if "candidate" in p.name]) == 1


def test_missing_or_symlink_roots_fail_without_creating_data(tmp_path: Path) -> None:
    with pytest.raises(V11CandidateError, match="non-symlink"):
        publish_v11_candidate(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
    root = _prepared(tmp_path)
    alias = tmp_path / "symlink"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(V11CandidateError, match="non-symlink"):
        publish_v11_candidate(alias)
    assert not (root / V11_CANDIDATE_FILENAME).exists()


@pytest.mark.parametrize("target", ["managed-state.json", "frozen-legacy-catalog.v2.json"])
def test_symlink_prerequisite_rejected_before_publication(tmp_path: Path, target: str) -> None:
    root = _prepared(tmp_path)
    path = root / target
    raw = path.read_bytes()
    outside = tmp_path / "outside"
    outside.write_bytes(raw)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(V11CandidateError, match="unsafe"):
        publish_v11_candidate(root)
    assert not (root / V11_CANDIDATE_FILENAME).exists()
    assert outside.read_bytes() == raw


def test_existing_conflicting_candidate_or_symlink_is_never_overwritten(tmp_path: Path) -> None:
    root = _prepared(tmp_path)
    marker = root / V11_CANDIDATE_FILENAME
    marker.write_bytes(b"untrusted different candidate")
    with pytest.raises(V11CandidateError, match="no overwrite"):
        publish_v11_candidate(root)
    assert marker.read_bytes() == b"untrusted different candidate"
    marker.unlink()
    marker.symlink_to(root / "managed-state.json")
    with pytest.raises(V11CandidateError, match="unsafe"):
        publish_v11_candidate(root)
    assert marker.is_symlink()


def test_missing_or_corrupted_frozen_v2_never_publishes(tmp_path: Path) -> None:
    root = _prepared(tmp_path)
    frozen = root / "frozen-legacy-catalog.v2.json"
    frozen.unlink()
    with pytest.raises(V11CandidateError):
        publish_v11_candidate(root)
    assert not (root / V11_CANDIDATE_FILENAME).exists()


def test_failure_before_exclusive_link_preserves_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _prepared(tmp_path)
    before = _files(root)

    def reject_link(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic exclusive link failure")

    monkeypatch.setattr(os, "link", reject_link)
    with pytest.raises(V11CandidateError, match="publication uncertain"):
        publish_v11_candidate(root)
    assert _files(root) == before


def test_concurrent_state_mutation_during_staging_aborts_without_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _prepared(tmp_path)
    original = os.fsync
    injected = False

    def inject_state_change(fd: int) -> None:
        nonlocal injected
        if not injected and stat.S_ISREG(os.fstat(fd).st_mode):
            injected = True
            with (root / "managed-state.json").open("ab") as stream:
                stream.write(b" ")
        original(fd)

    monkeypatch.setattr(os, "fsync", inject_state_change)
    with pytest.raises(V11CandidateError, match="legacy managed state changed"):
        publish_v11_candidate(root)
    assert (root / "managed-state.json").read_bytes().endswith(b" ")
    assert not (root / V11_CANDIDATE_FILENAME).exists()


def test_after_link_fsync_uncertainty_keeps_evidence_for_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _prepared(tmp_path)
    original = os.fsync
    failed = False

    def fail_first_directory_sync(fd: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError("synthetic uncertain durability")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail_first_directory_sync)
    with pytest.raises(V11CandidateError, match="publication uncertain"):
        publish_v11_candidate(root)
    assert (root / V11_CANDIDATE_FILENAME).is_file()
    assert any(".tmp" in item.name for item in root.iterdir())
    # Recovery inspection is read-only; a later operator can clean the temp.
    first = read_v11_candidate(root)
    assert first.original_managed_state_bytes == (root / "managed-state.json").read_bytes()
    assert publish_v11_candidate(root) == (root / V11_CANDIDATE_FILENAME).read_bytes()


def test_recovery_bytes_do_not_overwrite_changed_live_state(tmp_path: Path) -> None:
    root = _prepared(tmp_path)
    publish_v11_candidate(root)
    original = (root / "managed-state.json").read_bytes()
    (root / "managed-state.json").write_bytes(b"new live state simulated")
    before = _files(root)
    with pytest.raises(V11CandidateError, match="has changed"):
        read_v11_candidate(root)
    recovered = read_v11_candidate(root, require_legacy_unchanged=False)
    assert recovered.original_managed_state_bytes == original
    assert _files(root) == before
    with pytest.raises(V11CandidateError):
        publish_v11_candidate(root)
    assert _files(root) == before


def test_recovery_rejects_changed_frozen_or_candidate_evidence(tmp_path: Path) -> None:
    root = _prepared(tmp_path)
    publish_v11_candidate(root)
    marker = root / "frozen-legacy-catalog.v2.json"
    marker.write_bytes(b"corrupt frozen evidence")
    with pytest.raises(V11CandidateError, match="frozen-v2 evidence differ"):
        read_v11_candidate(root, require_legacy_unchanged=False)
    assert (root / V11_CANDIDATE_FILENAME).is_file()
