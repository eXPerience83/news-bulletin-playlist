"""Explicit offline normalization of an already-owned v11 managed overlay."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from news_bulletin_playlist.inspection_fence import _read
from news_bulletin_playlist.managed_provisioning import MANAGED_PROVISIONING_FILENAME
from news_bulletin_playlist.v11_candidate_storage import _locked_root
from news_bulletin_playlist.v11_operational_codec import (
    normalize_v11_overlay,
    parse_v11_operational,
)
from news_bulletin_playlist.v11_operational_storage import (
    V11OperationalStore,
    _authority_exists,
    _read_existing,
)

NORMALIZATION_CONFIRMATION = "I-STOPPED-RUNTIME-NORMALIZE-V11"


class OverlayNormalizationError(ValueError):
    """Do not change configuration when offline normalization cannot be verified."""


def normalize_offline(root: Path, *, expected_sha256: str, confirm: str) -> dict[str, object]:
    """Require operator-asserted offline exclusivity and publish via existing CAS.

    The root lock serializes owner writes, but not every runtime side effect or
    provisioning writer; the operator must stop all writers before invocation.
    """
    if confirm != NORMALIZATION_CONFIRMATION:
        raise OverlayNormalizationError("Explicit offline normalization confirmation is required")
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise OverlayNormalizationError("Provide the full lowercase v11 SHA-256")
    try:
        with _locked_root(root) as fd:
            gate = _read(fd)
            if gate is not None and gate[0] == "held":
                raise OverlayNormalizationError("Cannot normalize while inspection gate is held")
            if not _authority_exists(fd):
                raise OverlayNormalizationError("Existing v11 authority marker is required")
            try:
                os.stat(MANAGED_PROVISIONING_FILENAME, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise OverlayNormalizationError("Resolve pending provisioning before normalization")
            store = V11OperationalStore(root)
            before = _read_existing(fd)
            if before is None:
                raise OverlayNormalizationError("Existing v11 operational document is required")
            digest = hashlib.sha256(before).hexdigest()
            if digest != expected_sha256:
                raise OverlayNormalizationError("Stale v11 state SHA-256; inspect again")
            candidate = normalize_v11_overlay(before)
            previous = parse_v11_operational(before)
            next_state = parse_v11_operational(candidate)
            if candidate != before:
                published = store._save_locked(
                    fd, next_state.managed_overlay, expected_bytes=before
                )
                if published != candidate:
                    raise OverlayNormalizationError(
                        "Normalization publication differs; inspect state"
                    )
            return {
                "previous_schema": previous.managed_overlay.schema_version,
                "schema_version": next_state.managed_overlay.schema_version,
                "previous_sha256": digest,
                "state_sha256": hashlib.sha256(candidate).hexdigest(),
                "changed": candidate != before,
            }
    except OverlayNormalizationError:
        raise
    except (OSError, ValueError) as exc:
        raise OverlayNormalizationError(
            "Cannot safely normalize v11 overlay; inspect state"
        ) from exc
