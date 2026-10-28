"""Session-independent planning and one-state-commit playlist import; no provider I/O."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from news_bulletin_playlist.custom_cover_assets import CustomCoverAssetStore
from news_bulletin_playlist.custom_playlist_templates import LocalPlaylistTemplate
from news_bulletin_playlist.managed_provisioning import ProvisioningIntent, ProvisioningJournal
from news_bulletin_playlist.managed_state import (
    IMPORTED_PLAYLIST_SCHEMA_VERSION,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    effective_sources,
    serialize_managed_state,
)
from news_bulletin_playlist.models import CountryCode, LanguageTag, PlaylistId, SourceId
from news_bulletin_playlist.playlist_bundle import PlaylistBundle, inspect_playlist_bundle
from news_bulletin_playlist.portable_playlists import PortableCoverLoader, Synchronization
from news_bulletin_playlist.portable_sources import (
    PortableSource,
    UnsupportedPortableSource,
    parse_sources,
)
from news_bulletin_playlist.source_import import (
    SourceImportPreview,
    SourceImportRow,
    SourceImportService,
    plan_source_import,
    source_import_fingerprint,
)


class PlaylistImportError(ValueError):
    """Safe import conflict/durability message."""


@dataclass(frozen=True, slots=True)
class PlaylistImportPreview:
    bundle: PlaylistBundle
    sources: SourceImportPreview
    draft: LocalPlaylistTemplate | None
    status: str
    reason: str

    @property
    def blocked(self) -> bool:
        return self.status in {"unsupported", "uuid_conflict", "managed_uuid"}


@dataclass(frozen=True, slots=True)
class PlaylistImportResult:
    local_id: str
    created_sources: int
    created_draft: bool
    recovered: bool = False


class PlaylistImportService:
    def __init__(
        self,
        store: ManagedStateStore,
        synchronization: Synchronization,
        provisioning: ProvisioningJournal,
        covers: PortableCoverLoader,
    ) -> None:
        self.store, self.synchronization, self.provisioning, self.covers = (
            store,
            synchronization,
            provisioning,
            covers,
        )
        self.source_service = SourceImportService(store, synchronization, provisioning)

    def preview(self, document: bytes) -> PlaylistImportPreview:
        bundle = inspect_playlist_bundle(document)
        with self.synchronization.hold():
            state = self.store.load_readonly()
            return self._preview(bundle, state, self.provisioning.load())

    def _preview(
        self, bundle: PlaylistBundle, state: ManagedState, pending: ProvisioningIntent | None
    ) -> PlaylistImportPreview:
        sources = effective_sources(self.store.catalog, state)
        rows = tuple(
            sorted(
                (
                    *plan_source_import(
                        tuple(r for r in bundle.sources if isinstance(r, PortableSource)), sources
                    ),
                    *(
                        SourceImportRow(r, None, "unsupported")
                        for r in bundle.sources
                        if isinstance(r, UnsupportedPortableSource)
                    ),
                ),
                key=lambda r: r.spotify_show_id,
            )
        )
        source_preview = SourceImportPreview(
            rows,
            source_import_fingerprint(state, sources, pending, self.store.catalog),
            state.schema_version,
        )
        if source_preview.unsupported:
            return PlaylistImportPreview(
                bundle,
                source_preview,
                None,
                "unsupported",
                "Unsupported capabilities block the entire package",
            )
        uuid = bundle.playlist["portable_id"]
        if any(
            isinstance(raw := json.loads(q.raw_json), dict) and raw.get("portable_id") == uuid
            for q in state.quarantined_templates
        ):
            return PlaylistImportPreview(
                bundle,
                source_preview,
                None,
                "uuid_conflict",
                "UUID is reserved by a quarantined draft; repair it before importing",
            )
        if any(p.portable_id == uuid for p in state.playlists):
            return PlaylistImportPreview(
                bundle,
                source_preview,
                None,
                "managed_uuid",
                "UUID belongs to a managed playlist; no duplicate draft can be installed",
            )
        existing = next((t for t in state.templates if t.portable_id == uuid), None)
        occupied = (
            {str(t.id) for t in state.templates}
            | {str(t.id) for t in self.store.catalog.playlists}
            | {str(t.id) for t in state.playlists}
        )
        occupied.update(
            q.template_id for q in state.quarantined_templates if q.template_id is not None
        )
        if pending is not None:
            occupied.add(str(pending.template_id))
        base = "custom_" + uuid.replace("-", "")
        id_ = base
        suffix = 2
        while id_ in occupied:
            id_ = f"{base}_{suffix}"
            suffix += 1
        mapping = {r.incoming.id: r.local_id for r in rows if r.local_id is not None}
        p = bundle.playlist
        policy = p["duration_policy"]
        candidate = LocalPlaylistTemplate(
            id=existing.id if existing else PlaylistId(id_),
            portable_id=uuid,
            portable_metadata=bundle.metadata,
            display_name=p["display_name"],
            description=p["description"],
            default_source_ids=tuple(SourceId(mapping[s]) for s in p["source_ids"]),
            cover_id="uploaded_" + hashlib.sha256(bundle.jpeg).hexdigest(),
            countries=tuple(CountryCode(c) for c in p["countries"]),
            languages=tuple(LanguageTag(c) for c in p["languages"]),
            retention_hours=p["retention_hours"],
            max_episodes=p["max_episodes"],
            min_duration_seconds=policy["min_seconds"],
            max_duration_seconds=policy["max_seconds"],
            duration_exceptions=tuple(
                replace(e, source_id=SourceId(mapping[e.source_id])) for e in bundle.exceptions
            ),
            hidden=p["configuration"]["hidden"] if p["kind"] == "draft" else False,
        )
        if existing:
            data, attribution = self.covers.load(existing.cover_id)
            if existing.portable_metadata is not None:
                metadata = json.loads(existing.portable_metadata)
                if metadata["cover_sha256"] == hashlib.sha256(data).hexdigest():
                    attribution = metadata["attribution"]
            # Informational foreign IDs/origin flags are provenance, not executable content.
            # Compare fields directly: constructing without metadata would reimpose manual bounds.
            equal = all(
                getattr(existing, k) == getattr(candidate, k)
                for k in (
                    "display_name",
                    "description",
                    "default_source_ids",
                    "countries",
                    "languages",
                    "retention_hours",
                    "max_episodes",
                    "min_duration_seconds",
                    "max_duration_seconds",
                    "duration_exceptions",
                    "ordering",
                    "hidden",
                )
            )
            if not equal or data != bundle.jpeg or attribution != p["cover"]["attribution"]:
                return PlaylistImportPreview(
                    bundle,
                    source_preview,
                    candidate,
                    "uuid_conflict",
                    "UUID exists with different effective configuration or artwork",
                )
            return PlaylistImportPreview(
                bundle,
                source_preview,
                existing,
                "identical",
                "Existing UUID and effective configuration are identical; no writes",
            )
        full = self.source_service.build_candidate(state, rows, pending)
        full = replace(
            full,
            schema_version=IMPORTED_PLAYLIST_SCHEMA_VERSION,
            templates=full.templates + (candidate,),
        )
        serialize_managed_state(full, catalog=self.store.catalog)
        return PlaylistImportPreview(
            bundle,
            source_preview,
            candidate,
            "create",
            "Create one editable local draft; Spotify activation is blocked",
        )

    def apply(
        self, document: bytes, fingerprint: str, *, keep_local: frozenset[int], confirm_schema: bool
    ) -> PlaylistImportResult:
        with self.synchronization.hold():
            bundle = inspect_playlist_bundle(document)
            # Strict capability decoder remains the installation authority.
            parse_sources(bundle.source_document)
            state = self.store.load_readonly()
            pending = self.provisioning.load()
            actual_fingerprint = source_import_fingerprint(
                state, effective_sources(self.store.catalog, state), pending, self.store.catalog
            )
            if actual_fingerprint != fingerprint:
                raise PlaylistImportError(
                    "Configuration or pending provisioning changed; upload a new preview"
                )
            preview = self._preview(bundle, state, pending)
            if preview.blocked or preview.draft is None:
                raise PlaylistImportError(preview.reason)
            if keep_local != preview.sources.conflicts:
                raise PlaylistImportError(
                    "Every source conflict requires an explicit keep-local decision or cancel"
                )
            if preview.status == "identical":
                return PlaylistImportResult(str(preview.draft.id), 0, False)
            if state.schema_version < IMPORTED_PLAYLIST_SCHEMA_VERSION and not confirm_schema:
                raise PlaylistImportError(
                    "Explicit schema v10 compatibility and backup acknowledgement is required"
                )
            candidate = self.source_service.build_candidate(state, preview.sources.rows, pending)
            candidate = replace(
                candidate,
                schema_version=max(state.schema_version, IMPORTED_PLAYLIST_SCHEMA_VERSION),
                templates=tuple(sorted((*state.templates, preview.draft), key=lambda t: t.id)),
            )
            serialize_managed_state(candidate, catalog=self.store.catalog)
            CustomCoverAssetStore(self.covers.store.root).install_canonical(bundle.jpeg)
            creates = preview.sources.creates
            try:
                self.store.save(candidate)
            except OSError, ManagedStateError:
                try:
                    observed = self.store.load_readonly()
                except OSError, ManagedStateError:
                    raise PlaylistImportError(
                        "Import outcome is uncertain; readback failed. Do not retry blindly"
                    ) from None
                if observed == candidate:
                    return PlaylistImportResult(str(preview.draft.id), creates, True, True)
                if observed == state:
                    raise PlaylistImportError(
                        "Import was not committed; previous configuration is intact. "
                        "Prepared artwork can be safely reused"
                    ) from None
                raise PlaylistImportError(
                    "Import outcome is uncertain; inspect configuration before retrying"
                ) from None
            return PlaylistImportResult(str(preview.draft.id), creates, True)
