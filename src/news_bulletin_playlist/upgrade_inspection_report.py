"""Read-only v11 first-boot acceptance report for the operator.

Uses only the locked /data authority and inert legacy bytes. No OAuth,
Spotify, SQLite, scheduler or copy/backup is opened or created. The report
does not grant permission to release the inspection fence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from news_bulletin_playlist.custom_cover_assets import is_uploaded_cover_id
from news_bulletin_playlist.inspection_fence import InspectionGateError, _read
from news_bulletin_playlist.legacy_materialization import pause_dormant_legacy_playlists
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedStateError,
    compile_engine_config,
    parse_managed_state,
)
from news_bulletin_playlist.migrated_covers import MigratedCoverAssets, MigratedCoverError
from news_bulletin_playlist.portable_playlist_metadata import exception_payload
from news_bulletin_playlist.v11_candidate_storage import _locked_root, _read_regular
from news_bulletin_playlist.v11_operational_codec import (
    MAX_OPERATIONAL_BYTES,
    V11OperationalError,
    parse_v11_operational,
)
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME

REPORT_FORMAT = "news-bulletin-playlist.v11-inspection-report"


class UpgradeInspectionError(ValueError):
    """Cannot certify first-boot config integrity from local installation evidence."""


def upgrade_inspection_report(data_dir: Path) -> dict[str, object]:
    """Compare complete persisted v11 state to the inert original in one locked read.

    For an intentionally empty new installation, no original managed-state file
    exists, so historical equivalence is not applicable. Fail closed when an
    original exists but cannot be validated byte-for-byte.
    """
    try:
        with _locked_root(data_dir) as directory:
            gate = _read(directory)
            owned_raw = _read_regular(directory, V11_OPERATIONAL_FILENAME, MAX_OPERATIONAL_BYTES)
            operational = parse_v11_operational(owned_raw)
            try:
                os.stat(MANAGED_STATE_FILENAME, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                legacy_raw = None
            else:
                # Only a genuinely missing file is a clean-install candidate.
                # The checked reader must reject symlinks, FIFOs, permissions
                # and any other malformed existing legacy-file evidence.
                legacy_raw = _read_regular(directory, MANAGED_STATE_FILENAME, MAX_OPERATIONAL_BYTES)
        # All state-bearing inputs above were captured under the same lock.
        if legacy_raw is None:
            # Only a genuinely pristine first install lacks legacy bytes.
            # A migrated root without its original input cannot be verified:
            # never silently treat missing evidence as a clean install.
            if (
                operational.historical_catalog.sources
                or operational.historical_catalog.playlists
                or operational.installed_sources
                or operational.source_provenance
            ):
                raise UpgradeInspectionError("Original legacy managed-state is missing")
            legacy_matches: bool | None = None
            runtime_matches: bool | None = None
        else:
            if hashlib.sha256(legacy_raw).hexdigest() != operational.migration_origin_sha256:
                raise UpgradeInspectionError("Original managed-state bytes changed after migration")
            document = json.loads(legacy_raw.decode("utf-8"))
            previous = parse_managed_state(document, catalog=operational.historical_catalog)
            normalized = pause_dormant_legacy_playlists(operational.historical_catalog, previous)
            legacy_matches = normalized.playlists == operational.managed_overlay.playlists
            # The current owner catalog must reconstruct the historical engine
            # semantics, not merely preserve the Spotify destination IDs.
            original_runtime = compile_engine_config(operational.historical_catalog, normalized)
            runtime_matches = original_runtime == operational.runtime

        source_rows: list[dict[str, object]] = []
        for source in operational.runtime.sources:
            spotify_show = next(
                (
                    ref.external_id
                    for ref in source.external_references
                    if ref.system == "spotify" and ref.resource_type == "show"
                ),
                None,
            )
            source_rows.append(
                {
                    "id": source.id,
                    "spotify_show_id": spotify_show,
                    "enabled": source.enabled,
                    "parser_id": source.parser_id,
                    "collection_filter_id": source.collection_filter_id,
                    "temporal_strategy": source.temporal_strategy.value,
                    "public_release_mode": source.public_release_mode.value,
                    "timezone": source.timezone.key,
                }
            )
        playlists: list[dict[str, object]] = []
        for playlist in operational.managed_overlay.playlists:
            runtime = next(
                (item for item in operational.runtime.playlists if item.id == playlist.id),
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
                "id": template.id,
                "source_ids": list(template.default_source_ids),
                "cover_id": template.cover_id,
                "duration_exceptions": [
                    exception_payload(item) for item in template.duration_policy.exceptions
                ],
            }
            for template in operational.owner_inventory.templates.records
        ]
        cover_ids = sorted(
            {template.cover_id for template in operational.owner_inventory.templates.records}
            | {template.cover_id for template in operational.managed_overlay.templates}
            | {playlist.cover_id for playlist in operational.managed_overlay.playlists}
        )
        cover_ids = [cover_id for cover_id in cover_ids if not is_uploaded_cover_id(cover_id)]
        covers = MigratedCoverAssets(data_dir)
        artwork = [
            {"id": cover_id, "persisted_and_verified": covers.load(cover_id) is not None}
            for cover_id in cover_ids
        ]
        gate_state = "not_requested" if gate is None else gate[0]
        all_artwork = all(item["persisted_and_verified"] for item in artwork)
        checks: dict[str, bool] = {
            "owner_inventory_valid": True,
            "legacy_bytes_match_origin": (
                legacy_raw is None
                or hashlib.sha256(legacy_raw).hexdigest() == operational.migration_origin_sha256
            ),
            "managed_destinations_unchanged": legacy_matches is not False,
            "runtime_policies_unchanged": runtime_matches is not False,
            "historical_artwork_verified": all_artwork,
            "inspection_hold_active": gate_state == "held",
        }
        return {
            "format": REPORT_FORMAT,
            "version": 1,
            "gate": gate_state,
            "v11_state_sha256": hashlib.sha256(owned_raw).hexdigest(),
            "historical_origin_sha256": operational.migration_origin_sha256,
            "legacy_file_present": legacy_raw is not None,
            "legacy_comparison": (
                "not_applicable"
                if legacy_raw is None
                else "matching"
                if legacy_matches and runtime_matches
                else "changed"
            ),
            "checks": checks,
            "safe_to_review_without_spotify_writes": gate_state == "held" and all(checks.values()),
            "ready_for_production": False,
            "sources": source_rows,
            "playlists": playlists,
            "owner_templates": templates,
            "historical_covers": artwork,
        }
    except UpgradeInspectionError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        InspectionGateError,
        MigratedCoverError,
        ManagedStateError,
        V11OperationalError,
    ) as exc:
        raise UpgradeInspectionError(
            "Could not safely inspect and compare the v11 installation"
        ) from exc
