"""Versioned, opt-in legacy-catalog sidecar; not a managed-state migration (#552).

This separately invoked writer freezes installed source/template *definitions*.
It never edits managed-state.json, SQLite, OAuth, Spotify, or normal startup.
A digest ties the snapshot to the exact original managed-state bytes, but the
sidecar is NOT a full backup and must not be used as automatic write authority.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from news_bulletin_playlist.catalog import BuiltInCatalog, PlaylistTemplate
from news_bulletin_playlist.legacy_materialization import (
    FrozenLegacySource,
    FrozenLegacyTemplate,
    LegacyMigrationPreview,
    LegacyProvenance,
)
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedStateError,
    parse_managed_state,
    source_definition,
)
from news_bulletin_playlist.models import (
    CountryCode,
    DurationPolicy,
    DurationPolicyException,
    LanguageTag,
    OrderingPolicy,
    PlaylistId,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.portable_sources import (
    PortableSource,
    PortableSourceError,
    parse_sources,
    serialize_sources,
)

FROZEN_LEGACY_SNAPSHOT_FILENAME = "frozen-legacy-catalog.json"
FROZEN_LEGACY_SNAPSHOT_V2_FILENAME = "frozen-legacy-catalog.v2.json"
SNAPSHOT_FORMAT = "news-bulletin-playlist.frozen-legacy-catalog"
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_V2_SCHEMA_VERSION = 2
MAX_FROZEN_SNAPSHOT_BYTES = 1024 * 1024
MAX_MANAGED_STATE_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class FrozenLegacySnapshotError(ValueError):
    """Fail-closed snapshot error; not permission to migrate or provision."""


@dataclass(frozen=True, slots=True)
class FrozenLegacyCatalogSnapshot:
    managed_schema_version: int
    managed_state_sha256: str
    sources: tuple[FrozenLegacySource, ...]
    templates: tuple[FrozenLegacyTemplate, ...]
    source_order: tuple[SourceId, ...] | None = None


def build_legacy_snapshot(preview: LegacyMigrationPreview, state_bytes: bytes) -> bytes:
    """Build deterministic, bounded source/template definitions without any I/O."""
    if preview.original_state.schema_version not in (6, 7, 8, 9, 10):
        raise FrozenLegacySnapshotError("unsupported legacy schema for frozen catalog")
    if not isinstance(state_bytes, bytes) or len(state_bytes) > MAX_MANAGED_STATE_BYTES:
        raise FrozenLegacySnapshotError("invalid legacy state bytes")
    try:
        state = json.loads(state_bytes.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise FrozenLegacySnapshotError("invalid original state bytes") from exc
    if (
        not isinstance(state, dict)
        or type(state.get("schema_version")) is not int
        or state["schema_version"] != preview.original_state.schema_version
    ):
        raise FrozenLegacySnapshotError("original state schema mismatch")

    try:
        source_catalog = BuiltInCatalog(
            sources=tuple(
                source_definition(item.installed)
                for item in preview.source_snapshots
                if item.provenance is LegacyProvenance.IMAGE_PRESET
            ),
            playlists=tuple(
                item.definition
                for item in preview.template_snapshots
                if item.provenance is LegacyProvenance.IMAGE_PRESET
            ),
        )
        recovered = parse_managed_state(state, catalog=source_catalog)
    except (ManagedStateError, ValueError) as exc:
        raise FrozenLegacySnapshotError("original state cannot be revalidated") from exc
    if recovered != preview.original_state:
        raise FrozenLegacySnapshotError("original state differs from materialization preview")

    source_records = tuple(
        PortableSource(
            source_definition(record.installed),
            "built_in" if record.provenance is LegacyProvenance.IMAGE_PRESET else "custom",
        )
        for record in preview.source_snapshots
    )
    try:
        source_document = json.loads(serialize_sources(source_records))
    except (PortableSourceError, ValueError) as exc:
        raise FrozenLegacySnapshotError("unsupported frozen source capability") from exc

    document = {
        "format": SNAPSHOT_FORMAT,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "managed_schema_version": preview.original_state.schema_version,
        "managed_state_sha256": hashlib.sha256(state_bytes).hexdigest(),
        "sources": source_document,
        "templates": [_template_record(item) for item in preview.template_snapshots],
    }
    payload = _canonical(document)
    if len(payload) > MAX_FROZEN_SNAPSHOT_BYTES:
        raise FrozenLegacySnapshotError("frozen catalog exceeds size limit")
    loaded = parse_legacy_snapshot(payload)
    expected_sources = {item.installed.id: item for item in preview.source_snapshots}
    actual_sources = {item.installed.id: item for item in loaded.sources}
    if expected_sources != actual_sources or loaded.templates != preview.template_snapshots:
        raise FrozenLegacySnapshotError("frozen catalog is not lossless")
    return payload


def build_legacy_snapshot_v2(preview: LegacyMigrationPreview, state_bytes: bytes) -> bytes:
    """Record the precise historic effective source order without changing v1."""
    baseline = json.loads(build_legacy_snapshot(preview, state_bytes))
    ordered_ids = tuple(str(item.installed.id) for item in preview.source_snapshots)
    if ordered_ids != tuple(str(item.id) for item in preview.original_runtime.sources):
        raise FrozenLegacySnapshotError("legacy source order differs from runtime")
    baseline["schema_version"] = SNAPSHOT_V2_SCHEMA_VERSION
    baseline["source_order"] = list(ordered_ids)
    payload = _canonical(baseline)
    if len(payload) > MAX_FROZEN_SNAPSHOT_BYTES:
        raise FrozenLegacySnapshotError("frozen catalog exceeds size limit")
    parsed = parse_legacy_snapshot(payload)
    if parsed.source_order != tuple(item.installed.id for item in preview.source_snapshots):
        raise FrozenLegacySnapshotError("frozen source order cannot be verified")
    return payload


def parse_legacy_snapshot(payload: bytes) -> FrozenLegacyCatalogSnapshot:
    """Strict read-only decode: no trust in unverified provenance or file names."""
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_FROZEN_SNAPSHOT_BYTES:
        raise FrozenLegacySnapshotError("invalid frozen catalog size")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise FrozenLegacySnapshotError("invalid frozen catalog JSON") from exc
    if not isinstance(document, dict):
        raise FrozenLegacySnapshotError("invalid frozen catalog root")
    schema = document.get("schema_version")
    if type(schema) is not int or schema not in (
        SNAPSHOT_SCHEMA_VERSION,
        SNAPSHOT_V2_SCHEMA_VERSION,
    ):
        raise FrozenLegacySnapshotError("unsupported frozen catalog format")
    keys = {
        "format",
        "schema_version",
        "managed_schema_version",
        "managed_state_sha256",
        "sources",
        "templates",
    }
    if schema == SNAPSHOT_V2_SCHEMA_VERSION:
        keys.add("source_order")
    root = _record(document, keys)
    if root["format"] != SNAPSHOT_FORMAT:
        raise FrozenLegacySnapshotError("unsupported frozen catalog format")
    version = root["managed_schema_version"]
    if type(version) is not int or version not in (6, 7, 8, 9, 10):
        raise FrozenLegacySnapshotError("invalid frozen catalog managed schema")
    digest = root["managed_state_sha256"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise FrozenLegacySnapshotError("invalid frozen catalog state digest")

    try:
        source_payload = _canonical(root["sources"])
        definitions = parse_sources(source_payload)
    except (PortableSourceError, ValueError, TypeError) as exc:
        raise FrozenLegacySnapshotError("invalid frozen catalog sources") from exc
    sources = tuple(
        FrozenLegacySource(
            LegacyProvenance.IMAGE_PRESET
            if item.provenance == "built_in"
            else LegacyProvenance.INSTALLATION,
            _installed(item.source),
        )
        for item in definitions
    )
    raw_templates = root["templates"]
    if not isinstance(raw_templates, list) or len(raw_templates) > 100:
        raise FrozenLegacySnapshotError("invalid frozen catalog templates")
    templates = tuple(_parse_template(item) for item in raw_templates)
    template_ids = [item.definition.id for item in templates]
    ids = [item.installed.id for item in sources]
    source_ids = set(ids)
    if len(source_ids) != len(ids):
        raise FrozenLegacySnapshotError("duplicate frozen source IDs")
    source_order: tuple[SourceId, ...] | None = None
    if schema == SNAPSHOT_V2_SCHEMA_VERSION:
        raw_order = root["source_order"]
        if (
            not isinstance(raw_order, list)
            or len(raw_order) != len(ids)
            or any(type(item) is not str or not item for item in raw_order)
            or len(set(raw_order)) != len(raw_order)
            or set(raw_order) != {str(item) for item in ids}
        ):
            raise FrozenLegacySnapshotError("invalid frozen catalog source order")
        source_order = tuple(SourceId(item) for item in raw_order)
    if len(template_ids) != len(set(template_ids)):
        raise FrozenLegacySnapshotError("duplicate frozen template IDs")
    if any(
        source_id not in source_ids
        for item in templates
        for source_id in item.definition.default_source_ids
    ):
        raise FrozenLegacySnapshotError("frozen template references missing source")
    return FrozenLegacyCatalogSnapshot(version, digest, sources, templates, source_order)


def write_legacy_snapshot(data_dir: Path, preview: LegacyMigrationPreview) -> bytes:
    """Write the original v1 sidecar, never overwriting an existing file."""
    return _write_frozen_sidecar(
        data_dir, preview, FROZEN_LEGACY_SNAPSHOT_FILENAME, build_legacy_snapshot
    )


def write_legacy_snapshot_v2(data_dir: Path, preview: LegacyMigrationPreview) -> bytes:
    """Write v2 to a separate path, preserving any existing v1 evidence."""
    return _write_frozen_sidecar(
        data_dir, preview, FROZEN_LEGACY_SNAPSHOT_V2_FILENAME, build_legacy_snapshot_v2
    )


def _write_frozen_sidecar(
    data_dir: Path,
    preview: LegacyMigrationPreview,
    filename: str,
    builder: Callable[[LegacyMigrationPreview, bytes], bytes],
) -> bytes:
    """One-time, directory-locked, atomic, no-overwrite publication."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(data_dir, flags)
    except OSError as exc:
        raise FrozenLegacySnapshotError("legacy root must be an existing real directory") from exc
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        original = _read_regular(root_fd, "managed-state.json", MAX_MANAGED_STATE_BYTES)
        payload = builder(preview, original)
        if filename == FROZEN_LEGACY_SNAPSHOT_V2_FILENAME:
            try:
                prior_v1_bytes = _read_regular(
                    root_fd, FROZEN_LEGACY_SNAPSHOT_FILENAME, MAX_FROZEN_SNAPSHOT_BYTES
                )
            except FileNotFoundError:
                pass
            else:
                prior = parse_legacy_snapshot(prior_v1_bytes)
                current = parse_legacy_snapshot(payload)
                prior_sources = {item.installed.id: item for item in prior.sources}
                current_sources = {item.installed.id: item for item in current.sources}
                if (
                    prior.source_order is not None
                    or prior.managed_schema_version != current.managed_schema_version
                    or prior.managed_state_sha256 != current.managed_state_sha256
                    or prior_sources != current_sources
                    or prior.templates != current.templates
                ):
                    raise FrozenLegacySnapshotError(
                        "existing v1 snapshot conflicts with requested v2; no overwrite"
                    )
        try:
            previous = _read_regular(root_fd, filename, MAX_FROZEN_SNAPSHOT_BYTES)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if previous != payload:
                raise FrozenLegacySnapshotError("existing frozen snapshot differs; no overwrite")
            return previous

        temporary = f".frozen-legacy-catalog-{uuid4().hex}.tmp"
        linked = False
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if _read_regular(root_fd, "managed-state.json", MAX_MANAGED_STATE_BYTES) != original:
                raise FrozenLegacySnapshotError("managed state changed during snapshot")
            os.link(
                temporary,
                filename,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
                follow_symlinks=False,
            )
            linked = True
            os.fsync(root_fd)
            os.unlink(temporary, dir_fd=root_fd)
            os.fsync(root_fd)
            return payload
        finally:
            if not linked:
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=root_fd)
                    os.fsync(root_fd)
    except OSError as exc:
        raise FrozenLegacySnapshotError(
            "frozen catalog publication uncertain; inspect before retry"
        ) from exc
    finally:
        os.close(root_fd)


def _read_regular(dir_fd: int, name: str, maximum: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FrozenLegacySnapshotError("frozen catalog evidence is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise FrozenLegacySnapshotError("frozen catalog evidence is too large")
        return data
    finally:
        os.close(fd)


def _template_record(item: FrozenLegacyTemplate) -> dict[str, object]:
    t = item.definition
    return {
        "provenance": item.provenance.value,
        "id": str(t.id),
        "display_name": t.display_name,
        "description": t.description,
        "countries": list(t.countries),
        "languages": list(t.languages),
        "source_ids": list(t.default_source_ids),
        "cover_id": t.cover_id,
        "retention_hours": t.retention_hours,
        "max_episodes": t.max_episodes,
        "ordering": t.ordering.value,
        "duration_policy": {
            "min_seconds": t.duration_policy.default_min_seconds,
            "max_seconds": t.duration_policy.default_max_seconds,
            "exceptions": [
                {
                    "id": e.id,
                    "source_id": str(e.source_id),
                    "local_time": e.edition_local_time.isoformat(timespec="microseconds"),
                    "fold": e.edition_local_time.fold,
                    "max_seconds": e.max_seconds,
                }
                for e in t.duration_policy.exceptions
            ],
        },
    }


def _parse_template(value: object) -> FrozenLegacyTemplate:
    item = cast(
        dict[str, Any],
        _record(
            value,
            {
                "provenance",
                "id",
                "display_name",
                "description",
                "countries",
                "languages",
                "source_ids",
                "cover_id",
                "retention_hours",
                "max_episodes",
                "ordering",
                "duration_policy",
            },
        ),
    )
    try:
        provenance = LegacyProvenance(item["provenance"])
        if any(
            not isinstance(item[key], str)
            for key in ("id", "display_name", "description", "cover_id")
        ):
            raise ValueError("invalid text")
        if not all(
            isinstance(item[key], list) and all(isinstance(v, str) for v in item[key])
            for key in ("countries", "languages", "source_ids")
        ):
            raise ValueError("invalid sequence")
        if len(set(item["source_ids"])) != len(item["source_ids"]) or not item["source_ids"]:
            raise ValueError("invalid source selection")
        if any(type(item[key]) is not int for key in ("retention_hours", "max_episodes")):
            raise ValueError("invalid integer policy")
        policy = cast(
            dict[str, Any],
            _record(item["duration_policy"], {"min_seconds", "max_seconds", "exceptions"}),
        )
        if any(type(policy[key]) is not int for key in ("min_seconds", "max_seconds")):
            raise ValueError("invalid duration policy")
        if not isinstance(policy["exceptions"], list) or len(policy["exceptions"]) > 100:
            raise ValueError("invalid duration exceptions")
        exceptions = []
        for raw in policy["exceptions"]:
            e = cast(
                dict[str, Any],
                _record(raw, {"id", "source_id", "local_time", "fold", "max_seconds"}),
            )
            if not all(isinstance(e[k], str) for k in ("id", "source_id", "local_time")):
                raise ValueError("invalid duration exception")
            if type(e["fold"]) is not int or e["fold"] not in (0, 1):
                raise ValueError("invalid duration fold")
            if type(e["max_seconds"]) is not int:
                raise ValueError("invalid duration maximum")
            local_time = time.fromisoformat(e["local_time"])
            if local_time.tzinfo is not None:
                raise ValueError("time with timezone")
            exceptions.append(
                DurationPolicyException(
                    e["id"],
                    SourceId(e["source_id"]),
                    local_time.replace(fold=e["fold"]),
                    e["max_seconds"],
                )
            )
        ordering = OrderingPolicy(item["ordering"])
        if not item["id"] or not item["cover_id"] or not item["display_name"]:
            raise ValueError("missing template text")
        if type(item["retention_hours"]) is not int or item["retention_hours"] <= 0:
            raise ValueError("invalid retention")
        if type(item["max_episodes"]) is not int or item["max_episodes"] <= 0:
            raise ValueError("invalid max episodes")
        return FrozenLegacyTemplate(
            provenance,
            PlaylistTemplate(
                id=PlaylistId(item["id"]),
                display_name=item["display_name"],
                description=item["description"],
                countries=tuple(CountryCode(v) for v in item["countries"]),
                languages=tuple(LanguageTag(v) for v in item["languages"]),
                default_source_ids=tuple(SourceId(v) for v in item["source_ids"]),
                cover_id=item["cover_id"],
                retention_hours=item["retention_hours"],
                max_episodes=item["max_episodes"],
                ordering=ordering,
                duration_policy=DurationPolicy(
                    default_min_seconds=policy["min_seconds"],
                    default_max_seconds=policy["max_seconds"],
                    exceptions=tuple(exceptions),
                ),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FrozenLegacySnapshotError("invalid frozen catalog template") from exc


def _installed(source: SourceDefinition) -> InstalledSource:
    return InstalledSource.from_definition(source)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FrozenLegacySnapshotError("duplicate frozen catalog JSON field")
        result[key] = value
    return result


def _record(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise FrozenLegacySnapshotError("unexpected frozen catalog fields")
    return value


def _canonical(data: object) -> bytes:
    return (
        json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
