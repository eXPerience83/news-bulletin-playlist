"""Private offline rehearsal of portable recovery, never an installation restore."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from news_bulletin_playlist.configuration_export import (
    ConfigurationExport,
    export_configuration,
    inspect_configuration,
)
from news_bulletin_playlist.configuration_restore import (
    MAX_RESTORE_BYTES,
    ConfigurationRestoreError,
    ConfigurationRestoreService,
)
from news_bulletin_playlist.custom_cover_assets import CustomCoverAssetStore
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_provisioning import (
    MANAGED_PROVISIONING_FILENAME,
    ProvisioningJournal,
)
from news_bulletin_playlist.owner_inventory_export import inspect_owner_inventory
from news_bulletin_playlist.owner_template_records import OwnerTemplateSet
from news_bulletin_playlist.playlist_bundle import PlaylistBundle, inspect_playlist_bundle_v3
from news_bulletin_playlist.playlist_collection import inspect_collection
from news_bulletin_playlist.portable_playlists import canonical_json
from news_bulletin_playlist.v11_managed_adapter import ForwardOnlyManagedStateStore


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _members(package: ConfigurationExport) -> dict[str, PlaylistBundle]:
    return {
        member.portable_id: inspect_playlist_bundle_v3(member.document)
        for member in inspect_collection(package.collection_document)
    }


def _verify_owner(original: ConfigurationExport, recovered: ConfigurationExport) -> None:
    old = inspect_owner_inventory(original.owner_document)
    new = inspect_owner_inventory(recovered.owner_document)
    old_document = old.inventory.to_document()
    new_document = new.inventory.to_document()
    for field in ("installed_sources", "source_order", "retired_source_ids"):
        if canonical_json(old_document.get(field)) != canonical_json(new_document.get(field)):
            raise ConfigurationRestoreError("Rehearsal owner source configuration differs")

    remapped = {item.cover_id: "uploaded_" + _sha(item.jpeg) for item in old.artwork}
    expected_templates = OwnerTemplateSet(
        tuple(
            replace(template, cover_id=remapped[template.cover_id])
            for template in old.inventory.templates.records
        )
    )
    if expected_templates.serialize() != new.inventory.templates.serialize():
        raise ConfigurationRestoreError("Rehearsal owner template configuration differs")

    expected_art = {remapped[item.cover_id]: (item.jpeg, item.attribution) for item in old.artwork}
    actual_art = {item.cover_id: (item.jpeg, item.attribution) for item in new.artwork}
    if expected_art != actual_art:
        raise ConfigurationRestoreError("Rehearsal owner artwork differs")
    expected_credits = {key: json.loads(value) for key, value in old.inventory.cover_attributions}
    for cover_id, (_jpeg, credit) in expected_art.items():
        if expected_credits.setdefault(cover_id, credit) != credit:
            raise ConfigurationRestoreError("Rehearsal owner artwork credits conflict")
    actual_credits = {key: json.loads(value) for key, value in new.inventory.cover_attributions}
    if expected_credits != actual_credits:
        raise ConfigurationRestoreError("Rehearsal owner artwork attribution differs")


def _verify_members(original: ConfigurationExport, recovered: ConfigurationExport) -> None:
    old = _members(original)
    new = _members(recovered)
    if old.keys() != new.keys() or recovered.managed_links:
        raise ConfigurationRestoreError("Rehearsal playlist identities or activation differ")
    links = {link.playlist_uuid: link for link in original.managed_links}
    provenance = dict(original.draft_provenance)
    actual_provenance = dict(recovered.draft_provenance)
    if actual_provenance.keys() != old.keys():
        raise ConfigurationRestoreError("Rehearsal playlist provenance is incomplete")
    for identity, before in old.items():
        after = new[identity]
        ignored = {"kind", "original_id", "configuration", "cover"}
        old_policy = {k: v for k, v in before.playlist.items() if k not in ignored}
        new_policy = {k: v for k, v in after.playlist.items() if k not in ignored}
        if canonical_json(old_policy) != canonical_json(new_policy):
            raise ConfigurationRestoreError("Rehearsal playlist policy differs")
        if (
            before.jpeg != after.jpeg
            or before.playlist["cover"]["attribution"] != after.playlist["cover"]["attribution"]
        ):
            raise ConfigurationRestoreError("Rehearsal playlist artwork differs")
        is_draft = before.playlist["kind"] == "draft"
        hidden = before.playlist["configuration"]["hidden"] if is_draft else False
        if (
            after.playlist["kind"] != "draft"
            or after.playlist["configuration"] != {"enabled": None, "hidden": hidden}
            or (is_draft and before.playlist["original_id"] != after.playlist["original_id"])
        ):
            raise ConfigurationRestoreError("Rehearsal draft configuration differs")

        expected_metadata = json.loads(provenance.get(identity, before.metadata))
        template = before.template
        link = links.get(identity)
        if link is not None and link.template_kind == "draft":
            template = old[link.template_id].template
            expected_metadata["recovered_from_draft_uuid"] = link.template_id
        if (template is None) != (after.template is None) or (
            template is not None and after.template is not None and template.id != after.template.id
        ):
            raise ConfigurationRestoreError("Rehearsal owner template association differs")
        observed_metadata = json.loads(actual_provenance[identity])
        if canonical_json(expected_metadata) != canonical_json(observed_metadata):
            raise ConfigurationRestoreError("Rehearsal playlist provenance differs")


def rehearse_configuration(archive: Path) -> dict[str, object]:
    """Exercise the real restore/reopen/re-export path in a disposable local root.

    No destination data-dir parameter, runtime, credentials or provider client.
    Only aggregate counts and archive digests leave the temporary workspace.
    """
    try:
        with archive.open("rb") as stream:
            document = stream.read(MAX_RESTORE_BYTES + 1)
        if not 0 < len(document) <= MAX_RESTORE_BYTES:
            raise ConfigurationRestoreError("Configuration exceeds initial restore size limit")
        original = inspect_configuration(document)
        with TemporaryDirectory(prefix="news-playlist-rehearsal-") as directory:
            root = Path(directory)

            def service() -> ConfigurationRestoreService:
                return ConfigurationRestoreService(
                    ForwardOnlyManagedStateStore(root),
                    ConfigurationSynchronization(),
                    ProvisioningJournal(root / MANAGED_PROVISIONING_FILENAME),
                    CustomCoverAssetStore(root / "custom-covers"),
                )

            initial = service()
            preview = initial.preview(document)
            applied = initial.apply(document, preview.fingerprint, confirmed=True)
            if applied.observed_after_error:
                raise ConfigurationRestoreError("Rehearsal restore durability uncertain")
            # Reopen all stateful services; never reuse the original in-memory state.
            del initial
            restarted = service()
            state = restarted.store.load_readonly()
            if state.playlists or restarted.provisioning.load() is not None:
                raise ConfigurationRestoreError("Rehearsal unexpectedly created activation state")
            reexport = export_configuration(root, restarted.synchronization)
            recovered = inspect_configuration(reexport)
            _verify_owner(original, recovered)
            _verify_members(original, recovered)
            retry = restarted.preview(document)
            if not retry.already_restored:
                raise ConfigurationRestoreError("Rehearsal repeat preview is not idempotent")
            result = restarted.apply(document, retry.fingerprint, confirmed=True)
            if (
                not result.preview.already_restored
                or result.observed_after_error
                or export_configuration(root, restarted.synchronization) != reexport
            ):
                raise ConfigurationRestoreError("Rehearsal repeat apply changed configuration")
            return {
                "archive_sha256": _sha(document),
                "archive_bytes": len(document),
                "reexport_sha256": _sha(reexport),
                "sources": preview.sources,
                "owner_templates": preview.owner_templates,
                "original_drafts": preview.original_drafts,
                "recovered_managed": preview.recovered_managed,
                "active_destinations": 0,
                "idempotent": True,
            }
    except ConfigurationRestoreError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ConfigurationRestoreError("Isolated configuration rehearsal failed") from exc
