"""Explicit offline *shadow* v11 staging, never a live state cutover.

This primitive NEVER replaces managed-state.json and is NOT invoked by startup.
The operator assertion is not proof of app quiescence, SQLite consistency,
or snapshot recovery. Production use remains blocked pending those gates.
"""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from news_bulletin_playlist.legacy_catalog_snapshot import (
    MAX_FROZEN_SNAPSHOT_BYTES,
    MAX_MANAGED_STATE_BYTES,
)
from news_bulletin_playlist.offline_dataset_inventory import (
    DatasetInventoryError,
    OfflineDatasetInventory,
    inventory_offline_dataset,
)
from news_bulletin_playlist.v11_candidate import (
    MAX_CANDIDATE_BYTES,
    V11CandidateError,
    parse_v11_candidate,
)
from news_bulletin_playlist.v11_candidate_storage import (
    _locked_root,
    _read_regular,
)
from news_bulletin_playlist.v11_operational_codec import (
    MAX_OPERATIONAL_BYTES,
    V11OperationalError,
    V11OperationalState,
    build_v11_operational_from_candidate,
    parse_v11_operational,
)

V11_SHADOW_FILENAME = "managed-state-v11.shadow.json"


class V11ShadowStageError(ValueError):
    """A failed offline stage must never authorize migration."""


@dataclass(frozen=True, slots=True)
class V11ShadowInspection:
    state: V11OperationalState
    original_dataset: OfflineDatasetInventory
    shadow_bytes: bytes

    @property
    def permits_cutover(self) -> bool:
        return False


def _require_independent_backup(root: Path, backup: Path) -> None:
    try:
        source_path = root.resolve(strict=True)
        backup_path = backup.resolve(strict=True)
    except OSError as exc:
        raise V11ShadowStageError("missing offline dataset or backup") from exc
    if (
        source_path == backup_path
        or source_path in backup_path.parents
        or backup_path in source_path.parents
    ):
        raise V11ShadowStageError("backup must be an independent directory")


def _verified_offline_inventory(
    root: Path, backup: Path, *, extra_name: str | None = None
) -> OfflineDatasetInventory:
    _require_independent_backup(root, backup)
    try:
        source = inventory_offline_dataset(root)
        saved = inventory_offline_dataset(backup)
    except DatasetInventoryError as exc:
        raise V11ShadowStageError("offline dataset inventory is unsafe") from exc
    excluded = {V11_SHADOW_FILENAME}
    if extra_name is not None:
        excluded.add(extra_name)
    originals = tuple(item for item in source.files if item.path not in excluded)
    if source.directories != saved.directories or originals != saved.files:
        raise V11ShadowStageError("offline source differs from backup baseline")
    if sum(item.size for item in originals) != saved.total_bytes:
        raise V11ShadowStageError("offline byte totals differ from backup")
    for item in saved.files:
        source_file = root / item.path
        backup_file = backup / item.path
        left = source_file.stat(follow_symlinks=False)
        right = backup_file.stat(follow_symlinks=False)
        if (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino):
            raise V11ShadowStageError("hardlinked backup is not independent")
    return saved


def _shadow_evidence(dir_fd: int, root: Path, backup: Path) -> bytes:
    candidate_bytes = _read_regular(dir_fd, "managed-state-v11.candidate.json", MAX_CANDIDATE_BYTES)
    parsed_candidate = parse_v11_candidate(candidate_bytes)
    if _read_regular(dir_fd, "managed-state.json", MAX_MANAGED_STATE_BYTES) != (
        parsed_candidate.original_managed_state_bytes
    ):
        raise V11ShadowStageError("legacy state changed after candidate preparation")
    if _read_regular(dir_fd, "frozen-legacy-catalog.v2.json", MAX_FROZEN_SNAPSHOT_BYTES) != (
        parsed_candidate.frozen_catalog_v2_bytes
    ):
        raise V11ShadowStageError("frozen historical catalog has changed")
    _verified_offline_inventory(root, backup)
    try:
        return build_v11_operational_from_candidate(candidate_bytes)
    except V11OperationalError as exc:
        raise V11ShadowStageError("operational state does not match candidate") from exc


def stage_v11_shadow_state(
    root: Path, backup: Path, *, operator_asserts_offline: bool = False
) -> bytes:
    """Explicit shadow-only stage, requiring equal separate offline dataset.

    A valid shadow file does NOT change live selection, installation identity,
    or legacy managed-state. No startup or Admin caller is permitted here.
    """
    if not operator_asserts_offline:
        raise V11ShadowStageError("explicit offline operator assertion is required")
    with _locked_root(root) as root_fd:
        shadow = _shadow_evidence(root_fd, root, backup)
        try:
            os.stat(V11_SHADOW_FILENAME, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        else:
            try:
                current = _read_regular(root_fd, V11_SHADOW_FILENAME, MAX_OPERATIONAL_BYTES)
            except V11CandidateError as exc:
                raise V11ShadowStageError("unsafe existing v11 shadow evidence") from exc
        if current is not None:
            if current != shadow:
                raise V11ShadowStageError("existing v11 shadow conflicts; no overwrite")
            return current

        candidate_before = _read_regular(
            root_fd, "managed-state-v11.candidate.json", MAX_CANDIDATE_BYTES
        )
        tmp_name = f".managed-state-v11-shadow-{uuid4().hex}.tmp"
        linked = False
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(shadow)
                handle.flush()
                os.fsync(handle.fileno())
            # The extra temporary file is the *only* permitted inventory delta.
            _verified_offline_inventory(root, backup, extra_name=tmp_name)
            if _read_regular(
                root_fd, "managed-state-v11.candidate.json", MAX_CANDIDATE_BYTES
            ) != candidate_before:
                raise V11ShadowStageError("candidate changed during staging")
            os.link(
                tmp_name,
                V11_SHADOW_FILENAME,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
                follow_symlinks=False,
            )
            linked = True
            os.fsync(root_fd)
            os.unlink(tmp_name, dir_fd=root_fd)
            os.fsync(root_fd)
            return shadow
        except OSError as exc:
            raise V11ShadowStageError(
                "v11 shadow publication uncertain; inspect before retry"
            ) from exc
        finally:
            if not linked:
                with suppress(OSError):
                    os.unlink(tmp_name, dir_fd=root_fd)
                    os.fsync(root_fd)


def inspect_v11_shadow_state(root: Path, backup: Path) -> V11ShadowInspection:
    """Read-only proof the shadow equals the independently backed up candidate."""
    with _locked_root(root) as root_fd:
        raw = _read_regular(root_fd, V11_SHADOW_FILENAME, MAX_OPERATIONAL_BYTES)
        shadow = _shadow_evidence(root_fd, root, backup)
        if raw != shadow:
            raise V11ShadowStageError("shadow differs from verified candidate")
        return V11ShadowInspection(
            state=parse_v11_operational(raw),
            original_dataset=_verified_offline_inventory(root, backup),
            shadow_bytes=raw,
        )
