"""Read-only original v6-v10 inventory, before irreversible v11 startup.

No provider, OAuth, SQLite, temporary files, or /data authority writes.
The report is not a backup, migration, or authorization for Spotify writes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.custom_cover_assets import is_uploaded_cover_id
from news_bulletin_playlist.legacy_catalog_snapshot import MAX_MANAGED_STATE_BYTES
from news_bulletin_playlist.legacy_materialization import (
    pause_dormant_legacy_playlists,
    preview_legacy_materialization,
)
from news_bulletin_playlist.managed_state import MANAGED_STATE_FILENAME, parse_managed_state
from news_bulletin_playlist.portable_playlist_metadata import exception_payload
from news_bulletin_playlist.v11_candidate_storage import _locked_root, _read_regular
from news_bulletin_playlist.v11_operational_storage import (
    V11_AUTHORITY_FILENAME,
    V11_OPERATIONAL_FILENAME,
)

REPORT_FORMAT = "news-bulletin-playlist.legacy-preflight"


class LegacyPreflightError(ValueError):
    """Original legacy evidence cannot be inspected safely."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LegacyPreflightError("duplicate member in original legacy JSON")
        result[key] = value
    return result


def _exists(directory: int, filename: str) -> bool:
    try:
        os.stat(filename, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _assert_no_v11(directory: int) -> None:
    if _exists(directory, V11_AUTHORITY_FILENAME) or _exists(
        directory, V11_OPERATIONAL_FILENAME
    ):
        raise LegacyPreflightError("v11 authority exists; use inspection-report instead")


def legacy_preflight(
    data_dir: Path, *, expected_destinations: int | None = None
) -> dict[str, object]:
    """Compare one bounded, double-checked legacy snapshot without writing.

    Directory locking coordinates with the new migrator, but the deployed old
    process may not honor it. Operators must quiesce old writers separately.
    """
    if expected_destinations is not None and expected_destinations < 1:
        raise LegacyPreflightError("expected destination count must be positive")
    try:
        with _locked_root(data_dir) as directory:
            _assert_no_v11(directory)
            original = _read_regular(
                directory, MANAGED_STATE_FILENAME, MAX_MANAGED_STATE_BYTES
            )
            raw = json.loads(original.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
                raise LegacyPreflightError("invalid original legacy schema")
            if raw["schema_version"] not in {6, 7, 8, 9, 10}:
                raise LegacyPreflightError("only v6-v10 installations are supported")
            previous = parse_managed_state(raw, catalog=BUILTIN_CATALOG)
            preview = preview_legacy_materialization(BUILTIN_CATALOG, previous)
            normalized = pause_dormant_legacy_playlists(BUILTIN_CATALOG, previous)

            sources: list[dict[str, object]] = []
            for source in preview.original_runtime.sources:
                show_id = next(
                    (
                        ref.external_id
                        for ref in source.external_references
                        if ref.system == "spotify" and ref.resource_type == "show"
                    ),
                    None,
                )
                sources.append(
                    {
                        "id": source.id,
                        "spotify_show_id": show_id,
                        "enabled": source.enabled,
                        "parser_id": source.parser_id,
                        "collection_filter_id": source.collection_filter_id,
                        "temporal_strategy": source.temporal_strategy.value,
                        "public_release_mode": source.public_release_mode.value,
                        "timezone": source.timezone.key,
                    }
                )

            playlists: list[dict[str, object]] = []
            for playlist in normalized.playlists:
                runtime = next(
                    (
                        item
                        for item in preview.original_runtime.playlists
                        if item.id == playlist.id
                    ),
                    None,
                )
                playlists.append(
                    {
                        "id": playlist.id,
                        "template_id": playlist.template_id,
                        "name": playlist.display_name,
                        "enabled": playlist.enabled,
                        "spotify_playlist_id": playlist.destination.external_id,
                        "source_ids": list(playlist.source_ids),
                        "cover_id": playlist.cover_id,
                        "retention_hours": playlist.retention_hours,
                        "max_episodes": playlist.max_episodes,
                        "min_duration_seconds": playlist.min_duration_seconds,
                        "max_duration_seconds": playlist.max_duration_seconds,
                        "effective_edition_exceptions": (
                            [exception_payload(item) for item in runtime.duration_policy.exceptions]
                            if runtime is not None
                            else []
                        ),
                    }
                )

            templates = [
                {
                    "id": item.definition.id,
                    "source_ids": list(item.definition.default_source_ids),
                    "cover_id": item.definition.cover_id,
                    "duration_exceptions": [
                        exception_payload(exc)
                        for exc in item.definition.duration_policy.exceptions
                    ],
                }
                for item in preview.template_snapshots
            ]
            historical_ids = (
                {item.definition.cover_id for item in preview.template_snapshots}
                | {item.cover_id for item in previous.playlists}
                | {item.cover_id for item in previous.templates}
            )
            cover_ids = sorted(
                cover_id for cover_id in historical_ids if not is_uploaded_cover_id(cover_id)
            )

            if _read_regular(
                directory, MANAGED_STATE_FILENAME, MAX_MANAGED_STATE_BYTES
            ) != original:
                raise LegacyPreflightError("original legacy state changed during inspection")
            _assert_no_v11(directory)

            matches = (
                None
                if expected_destinations is None
                else len(playlists) == expected_destinations
            )
            return {
                "format": REPORT_FORMAT,
                "version": 1,
                "read_only": True,
                "ready_for_production": False,
                "legacy_schema_version": raw["schema_version"],
                "legacy_state_sha256": hashlib.sha256(original).hexdigest(),
                "destination_count": len(playlists),
                "expected_destination_count": expected_destinations,
                "destination_count_matches_expectation": matches,
                "source_count": len(sources),
                "template_count": len(templates),
                "quarantined_template_count": len(previous.quarantined_templates),
                "sources": sources,
                "playlists": playlists,
                "owner_templates": templates,
                "historical_cover_ids": cover_ids,
            }
    except LegacyPreflightError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise LegacyPreflightError("cannot safely inspect original legacy installation") from exc
