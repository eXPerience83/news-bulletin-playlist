"""Pure, bounded schema-v11 *candidate* envelope; not the live state codec.

The envelope contains separately typed ownership sections, exact legacy
managed-state bytes for rollback, and the frozen ordered v2 catalog. No
startup integration, persistence, activation, or live provider effects.
The SHA-256 fields detect accidental mismatch; they are not signatures.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass

from news_bulletin_playlist.legacy_catalog_snapshot import (
    MAX_FROZEN_SNAPSHOT_BYTES,
    MAX_MANAGED_STATE_BYTES,
    FrozenLegacySnapshotError,
    parse_legacy_snapshot,
)
from news_bulletin_playlist.v11_projection import V11LegacyProjection, project_v11_legacy

CANDIDATE_FORMAT = "news-bulletin-playlist.managed-state-candidate"
CANDIDATE_SCHEMA_VERSION = 11
MAX_CANDIDATE_BYTES = 24 * 1024 * 1024


class V11CandidateError(ValueError):
    """An invalid candidate must never become authority to modify live state."""


@dataclass(frozen=True, slots=True)
class V11Candidate:
    """Validated candidate, with exact old bytes held solely for future rollback."""

    original_managed_state_bytes: bytes
    frozen_catalog_v2_bytes: bytes
    projection: V11LegacyProjection


def build_v11_candidate(frozen_v2: bytes, original_state: bytes) -> bytes:
    """Construct a deterministic self-contained candidate without disk writes."""
    return _canonical(_candidate_document(frozen_v2, original_state))


def parse_v11_candidate(payload: bytes) -> V11Candidate:
    """Verify every section against original bytes; accept only canonical v11."""
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_CANDIDATE_BYTES:
        raise V11CandidateError("invalid v11 candidate size")
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise V11CandidateError("invalid v11 candidate JSON") from exc
    if not isinstance(document, dict) or set(document) != {
        "format",
        "schema_version",
        "legacy_schema_version",
        "legacy_managed_state_sha256",
        "frozen_catalog_v2_sha256",
        "original_state_base64",
        "frozen_catalog_base64",
        "ownership",
    }:
        raise V11CandidateError("invalid v11 candidate fields")
    if (
        document["format"] != CANDIDATE_FORMAT
        or type(document["schema_version"]) is not int
        or document["schema_version"] != CANDIDATE_SCHEMA_VERSION
    ):
        raise V11CandidateError("unsupported v11 candidate format")
    original = _decode_exact(document["original_state_base64"], MAX_MANAGED_STATE_BYTES)
    frozen = _decode_exact(document["frozen_catalog_base64"], MAX_FROZEN_SNAPSHOT_BYTES)
    expected = _candidate_document(frozen, original)
    if payload != _canonical(expected):
        raise V11CandidateError("v11 candidate differs from verified original records")
    return V11Candidate(original, frozen, project_v11_legacy(frozen, original))


def _candidate_document(frozen_v2: bytes, original_state: bytes) -> dict[str, object]:
    if (
        not isinstance(frozen_v2, bytes)
        or not 0 < len(frozen_v2) <= MAX_FROZEN_SNAPSHOT_BYTES
        or not isinstance(original_state, bytes)
        or not 0 < len(original_state) <= MAX_MANAGED_STATE_BYTES
    ):
        raise V11CandidateError("invalid candidate input sizes")
    try:
        snapshot = parse_legacy_snapshot(frozen_v2)
        if snapshot.source_order is None:
            raise V11CandidateError("ordered frozen v2 is required for v11 candidates")
        projection = project_v11_legacy(frozen_v2, original_state)
        if not projection.source_order_verified:
            raise V11CandidateError("legacy source order cannot be verified")
        frozen_doc = json.loads(
            frozen_v2.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        legacy_doc = json.loads(
            original_state.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except V11CandidateError:
        raise
    except (FrozenLegacySnapshotError, ValueError, UnicodeError, RecursionError) as exc:
        raise V11CandidateError("candidate does not reproduce a valid legacy state") from exc
    if not isinstance(frozen_doc, dict) or not isinstance(legacy_doc, dict):
        raise V11CandidateError("invalid candidate source records")
    ownership = {
        "installed_sources": frozen_doc["sources"]["sources"],
        "source_order": [str(source_id) for source_id in snapshot.source_order],
        "playlist_examples": frozen_doc["templates"],
        "local_drafts": legacy_doc.get("templates", []),
        "managed_destinations": legacy_doc["playlists"],
        "source_overrides": legacy_doc["source_overrides"],
        "quarantined_drafts": legacy_doc.get("quarantined_templates", []),
    }
    return {
        "format": CANDIDATE_FORMAT,
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "legacy_schema_version": projection.original_state.schema_version,
        "legacy_managed_state_sha256": hashlib.sha256(original_state).hexdigest(),
        "frozen_catalog_v2_sha256": hashlib.sha256(frozen_v2).hexdigest(),
        "original_state_base64": base64.b64encode(original_state).decode("ascii"),
        "frozen_catalog_base64": base64.b64encode(frozen_v2).decode("ascii"),
        "ownership": ownership,
    }


def _decode_exact(value: object, limit: int) -> bytes:
    if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
        raise V11CandidateError("invalid encoded candidate record")
    try:
        result = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise V11CandidateError("invalid candidate base64") from exc
    if not 0 < len(result) <= limit or base64.b64encode(result).decode("ascii") != value:
        raise V11CandidateError("invalid candidate decoded record")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise V11CandidateError("duplicate v11 candidate field")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise V11CandidateError("invalid numeric constant in v11 candidate")


def _canonical(value: object) -> bytes:
    encoded = (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_CANDIDATE_BYTES:
        raise V11CandidateError("v11 candidate exceeds size limit")
    return encoded
