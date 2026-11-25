"""Offline, confirmed restoration into a fresh v11 authority; no provider activation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from news_bulletin_playlist.configuration_export import (
    ConfigurationExport,
    inspect_configuration,
)
from news_bulletin_playlist.custom_cover_assets import (
    CustomCoverAssetStore,
    is_uploaded_cover_id,
)
from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal
from news_bulletin_playlist.managed_state import (
    PLAYLIST_ORDERING_SCHEMA_VERSION,
    ManagedState,
    ManagedStateError,
)
from news_bulletin_playlist.models import (
    CountryCode,
    LanguageTag,
    OrderingPolicy,
    PlaylistId,
    SourceId,
)
from news_bulletin_playlist.owner_inventory import OwnerInventory
from news_bulletin_playlist.owner_inventory_export import inspect_owner_inventory
from news_bulletin_playlist.owner_template_records import OwnerTemplateSet
from news_bulletin_playlist.playlist_bundle import inspect_playlist_bundle_v3
from news_bulletin_playlist.playlist_collection import inspect_collection
from news_bulletin_playlist.portable_playlists import Synchronization
from news_bulletin_playlist.v11_managed_adapter import ForwardOnlyManagedStateStore
from news_bulletin_playlist.v11_operational_codec import (
    parse_v11_operational,
    update_v11_owner_inventory,
)

# A deliberately narrower initial restore bound than the read-only export limit.
# Do not expose this service to HTTP uploads until peak memory is measured.
MAX_RESTORE_BYTES = 16 * 1024 * 1024


class ConfigurationRestoreError(ValueError):
    """Safe, local restore refusal or uncertain commit."""


@dataclass(frozen=True, slots=True)
class RestorePreview:
    fingerprint: str
    sources: int
    owner_templates: int
    original_drafts: int
    recovered_managed: int
    already_restored: bool


@dataclass(frozen=True, slots=True)
class RestoreResult:
    preview: RestorePreview
    observed_after_error: bool = False


@dataclass(frozen=True, slots=True)
class _Candidate:
    inventory: OwnerInventory
    overlay: ManagedState
    assets: tuple[tuple[str, bytes], ...]
    preview: RestorePreview
    expected_bytes: bytes


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inspect(document: bytes) -> ConfigurationExport:
    if not isinstance(document, bytes) or not 0 < len(document) <= MAX_RESTORE_BYTES:
        raise ConfigurationRestoreError("Configuration exceeds initial restore size limit")
    try:
        return inspect_configuration(document)
    except (ValueError, OSError) as exc:
        raise ConfigurationRestoreError("Invalid portable configuration") from exc


def _credit(attribution: dict[str, object]) -> str:
    encoded = json.dumps(attribution, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded + "\n"


def _build(
    package: ConfigurationExport, schema: int
) -> tuple[OwnerInventory, ManagedState, tuple[tuple[str, bytes], ...]]:
    owner = inspect_owner_inventory(package.owner_document)
    if any(link.template_kind != "owner" for link in package.managed_links):
        # The current draft model has owner links, not draft-to-draft links.
        # Never silently flatten a shared local template or fabricate an owner record.
        raise ConfigurationRestoreError("Managed-to-local-draft restoration is not supported yet")
    links = {link.playlist_uuid: link.template_id for link in package.managed_links}
    credits = dict(owner.inventory.cover_attributions)
    assets: dict[str, bytes] = {}
    remapped: dict[str, str] = {}
    original_credits: dict[str, dict[str, object]] = {}

    def asset(original_id: str, jpeg: bytes) -> str:
        cover_id = "uploaded_" + _sha(jpeg)
        if is_uploaded_cover_id(original_id) and original_id != cover_id:
            raise ConfigurationRestoreError("Uploaded cover identity differs from its bytes")
        if assets.setdefault(cover_id, jpeg) != jpeg:
            raise ConfigurationRestoreError("Conflicting content-addressed artwork")
        return cover_id

    for artwork in owner.artwork:
        cover_id = asset(artwork.cover_id, artwork.jpeg)
        remapped[artwork.cover_id] = cover_id
        original_credits[artwork.cover_id] = artwork.attribution
        encoded = _credit(artwork.attribution)
        if credits.setdefault(cover_id, encoded) != encoded:
            raise ConfigurationRestoreError("Conflicting owner artwork attribution")

    inventory = replace(
        owner.inventory,
        templates=OwnerTemplateSet(
            tuple(
                replace(template, cover_id=remapped[template.cover_id])
                for template in owner.inventory.templates.records
            )
        ),
        cover_attributions=tuple(sorted(credits.items())),
    )
    occupied = {template.id for template in inventory.templates.records}
    drafts = []
    for member in inspect_collection(package.collection_document):
        bundle = inspect_playlist_bundle_v3(member.document)
        p = bundle.playlist
        template = bundle.template
        if (
            template is not None
            and original_credits.get(template.cover_id) != bundle.template_attribution
        ):
            raise ConfigurationRestoreError("Template artwork attribution differs from inventory")
        owner_id: PlaylistId | None
        if member.kind == "managed":
            local_id = PlaylistId("custom_" + member.portable_id.replace("-", "") + "_recovered")
            owner_id = PlaylistId(links[member.portable_id])
        else:
            local_id = PlaylistId(p["original_id"])
            owner_id = template.id if template is not None else None
        if local_id in occupied:
            raise ConfigurationRestoreError("Restored draft identity conflicts with another record")
        occupied.add(local_id)
        policy = p["duration_policy"]
        override = p["ordering_override"]
        drafts.append(
            LocalPlaylistTemplate(
                id=local_id,
                portable_id=member.portable_id,
                portable_metadata=bundle.metadata,
                display_name=p["display_name"],
                description=p["description"],
                default_source_ids=tuple(SourceId(x) for x in p["source_ids"]),
                cover_id=asset(p["cover"]["original_id"], bundle.jpeg),
                countries=tuple(CountryCode(x) for x in p["countries"]),
                languages=tuple(LanguageTag(x) for x in p["languages"]),
                retention_hours=p["retention_hours"],
                max_episodes=p["max_episodes"],
                min_duration_seconds=policy["min_seconds"],
                max_duration_seconds=policy["max_seconds"],
                duration_exceptions=bundle.exceptions,
                hidden=p["configuration"]["hidden"] if member.kind == "draft" else False,
                owner_template_id=owner_id,
                ordering_override=OrderingPolicy(override) if override is not None else None,
            )
        )
    overlay = ManagedState(
        schema_version=max(schema, PLAYLIST_ORDERING_SCHEMA_VERSION),
        templates=tuple(sorted(drafts, key=lambda draft: draft.id)),
    )
    return inventory, overlay, tuple(sorted(assets.items()))


class ConfigurationRestoreService:
    """Backend-only restore gate; explicit preview and confirmation, one owner CAS."""

    def __init__(
        self,
        store: ForwardOnlyManagedStateStore,
        synchronization: Synchronization,
        provisioning: ProvisioningJournal,
        assets: CustomCoverAssetStore,
    ) -> None:
        self.store = store
        self.synchronization = synchronization
        self.provisioning = provisioning
        self.assets = assets

    def _candidate(self, document: bytes, package: ConfigurationExport) -> _Candidate:
        state = self.store.load_readonly()
        previous = self.store._expected_bytes
        current = parse_v11_operational(previous)
        if (
            not current.owner_sources_complete
            or current.historical_catalog.sources
            or current.historical_catalog.playlists
            or current.inherited_cover_ids
            or self.provisioning.load() is not None
        ):
            raise ConfigurationRestoreError("Restore requires a fresh v11 authority without intent")
        inventory, overlay, assets = _build(package, state.schema_version)
        identical = current.owner_inventory == inventory and state == overlay
        if not identical and (
            current.owner_inventory.sources
            or current.owner_inventory.templates.records
            or current.owner_inventory.cover_attributions
            or current.owner_inventory.retired_source_ids
            or state != ManagedState(schema_version=state.schema_version)
        ):
            raise ConfigurationRestoreError(
                "Restore refuses to overwrite an existing configuration"
            )
        expected = update_v11_owner_inventory(previous, inventory, managed_overlay=overlay)
        fingerprint = _sha(hashlib.sha256(document).digest() + hashlib.sha256(previous).digest())
        preview = RestorePreview(
            fingerprint,
            len(inventory.sources),
            len(inventory.templates.records),
            len(overlay.templates) - len(package.managed_links),
            len(package.managed_links),
            identical,
        )
        return _Candidate(inventory, overlay, assets, preview, expected)

    def preview(self, document: bytes) -> RestorePreview:
        package = _inspect(document)
        try:
            with self.synchronization.hold():
                return self._candidate(document, package).preview
        except ConfigurationRestoreError:
            raise
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise ConfigurationRestoreError("Restore preview could not be prepared safely") from exc

    def apply(
        self, document: bytes, fingerprint: str, *, confirmed: bool = False
    ) -> RestoreResult:
        if confirmed is not True:
            raise ConfigurationRestoreError("Explicit restore confirmation is required")
        package = _inspect(document)
        try:
            with self.synchronization.hold():
                candidate = self._candidate(document, package)
                if candidate.preview.fingerprint != fingerprint:
                    raise ConfigurationRestoreError(
                        "Restore preview is stale or belongs to another ZIP"
                    )
                if candidate.preview.already_restored:
                    for cover_id, jpeg in candidate.assets:
                        if self.assets.load(cover_id) != jpeg:
                            raise ConfigurationRestoreError("Restored artwork differs")
                    return RestoreResult(candidate.preview)
                previous = self.store._expected_bytes
                for cover_id, jpeg in candidate.assets:
                    if self.assets.install_canonical(jpeg) != cover_id:
                        raise ConfigurationRestoreError("Prepared artwork identity differs")
                # Only immutable content-addressed assets may precede the single configuration CAS.
                try:
                    self.store.save_owner_bundle(candidate.inventory, candidate.overlay)
                except (ManagedStateError, OSError):
                    try:
                        self.store.load_readonly()
                        observed = self.store._expected_bytes
                    except (ValueError, OSError):
                        raise ConfigurationRestoreError(
                            "Restore outcome uncertain; inspect authority before retrying"
                        ) from None
                    if observed == candidate.expected_bytes:
                        return RestoreResult(candidate.preview, observed_after_error=True)
                    if observed == previous:
                        raise ConfigurationRestoreError(
                            "Restore not committed; prepared artwork may be reused"
                        ) from None
                    raise ConfigurationRestoreError(
                        "Restore outcome uncertain; inspect authority before retrying"
                    ) from None
                return RestoreResult(candidate.preview)
        except ConfigurationRestoreError:
            raise
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise ConfigurationRestoreError("Configuration restore failed safely") from exc
