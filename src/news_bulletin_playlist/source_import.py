"""Local-only source import planning and one-commit apply; no provider dependencies."""

from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager
from dataclasses import dataclass, fields, replace
from typing import Protocol

from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.custom_playlist_templates import effective_playlist_templates
from news_bulletin_playlist.managed_provisioning import ProvisioningIntent, ProvisioningJournal
from news_bulletin_playlist.managed_state import (
    INSTALLED_SOURCES_SCHEMA_VERSION,
    InstalledSource,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    compile_engine_config,
    effective_sources,
    serialize_managed_state,
    template_suspended_by_quarantine,
)
from news_bulletin_playlist.models import SourceDefinition, SourceId
from news_bulletin_playlist.portable_sources import (
    PortableSource,
    UnsupportedPortableSource,
    UnsupportedSourceCapability,
    inspect_sources,
    parse_sources,
)


class _Synchronization(Protocol):
    def hold(self) -> AbstractContextManager[None]: ...


class SourceImportError(ValueError):
    """Sanitized error whose message may be shown to an authenticated operator."""


class StaleSourceImport(SourceImportError):
    """A new preview is needed after configuration or pending provisioning changed."""


class SourceImportWriteError(SourceImportError):
    """Save failed; the message distinguishes unchanged from uncertain state."""


@dataclass(frozen=True, slots=True)
class SourceImportRow:
    incoming: SourceDefinition | UnsupportedPortableSource
    local_id: SourceId | None
    status: str
    local: SourceDefinition | None = None
    differences: tuple[str, ...] = ()

    @property
    def spotify_show_id(self) -> str:
        if isinstance(self.incoming, UnsupportedPortableSource):
            return self.incoming.spotify_show_id
        return self.incoming.external_references[0].external_id


@dataclass(frozen=True, slots=True)
class SourceImportPreview:
    rows: tuple[SourceImportRow, ...]
    fingerprint: str
    schema_version: int

    @property
    def unsupported(self) -> int:
        return sum(row.status == "unsupported" for row in self.rows)

    @property
    def creates(self) -> int:
        return sum(row.status == "create" for row in self.rows)

    @property
    def conflicts(self) -> frozenset[int]:
        return frozenset(i for i, row in enumerate(self.rows) if row.status == "conflict")


@dataclass(frozen=True, slots=True)
class SourceImportResult:
    rows: tuple[SourceImportRow, ...]
    created: int
    recovered: bool = False


def _differences(left: SourceDefinition, right: SourceDefinition) -> tuple[str, ...]:
    # All domain fields participate, including ordered classifications. Only logical
    # identity is foreign; capability semantics never depend on claimed provenance.
    return tuple(
        field.name
        for field in fields(SourceDefinition)
        if field.name != "id"
        and (
            left.timezone.key != right.timezone.key
            if field.name == "timezone"
            else getattr(left, field.name) != getattr(right, field.name)
        )
    )


def _plan(
    records: tuple[PortableSource, ...], local_sources: tuple[SourceDefinition, ...]
) -> tuple[SourceImportRow, ...]:
    by_show = {source.external_references[0].external_id: source for source in local_sources}
    occupied = {source.id for source in local_sources}
    rows: list[SourceImportRow] = []
    # Input order cannot influence allocation or the displayed original -> local map.
    for record in sorted(records, key=lambda item: item.source.external_references[0].external_id):
        incoming = record.source
        show = incoming.external_references[0].external_id
        local = by_show.get(show)
        if local is not None:
            differences = _differences(incoming, local)
            status = (
                "conflict" if differences else ("identical" if incoming.id == local.id else "reuse")
            )
            rows.append(SourceImportRow(incoming, local.id, status, local, differences))
        else:
            base = f"custom_{show}"
            local_id = SourceId(base)
            suffix = 2
            while local_id in occupied:
                local_id = SourceId(f"{base}_{suffix}")
                suffix += 1
            occupied.add(local_id)
            rows.append(SourceImportRow(incoming, local_id, "create"))
    return tuple(rows)


def _fingerprint(
    state: ManagedState,
    sources: tuple[SourceDefinition, ...],
    pending: ProvisioningIntent | None,
    catalog: BuiltInCatalog,
) -> str:
    # Immutable domain snapshots include destinations, drafts/quarantine and all
    # reference/policy data. Nothing from this representation is logged or exported.
    return hashlib.sha256(repr((state, sources, pending, catalog)).encode("utf-8")).hexdigest()


class SourceImportService:
    def __init__(
        self,
        store: ManagedStateStore,
        synchronization: _Synchronization,
        provisioning: ProvisioningJournal,
    ) -> None:
        self.store = store
        self.synchronization = synchronization
        self.provisioning = provisioning

    def preview(self, document: bytes) -> SourceImportPreview:
        inspected = inspect_sources(document)
        records = tuple(item for item in inspected if isinstance(item, PortableSource))
        unsupported = tuple(
            SourceImportRow(item, None, "unsupported")
            for item in inspected
            if isinstance(item, UnsupportedPortableSource)
        )
        with self.synchronization.hold():
            state = self.store.load_readonly()
            pending = self.provisioning.load()
            sources = effective_sources(self.store.catalog, state)
            rows = tuple(
                sorted(
                    (*_plan(records, sources), *unsupported), key=lambda row: row.spotify_show_id
                )
            )
            if not unsupported and any(row.status == "create" for row in rows):
                self._candidate(state, rows, pending)
            return SourceImportPreview(
                rows,
                _fingerprint(state, sources, pending, self.store.catalog),
                state.schema_version,
            )

    def apply(
        self, document: bytes, fingerprint: str, *, keep_local: frozenset[int], confirm_v8: bool
    ) -> SourceImportResult:
        records = parse_sources(document)
        with self.synchronization.hold():
            state = self.store.load_readonly()
            pending = self.provisioning.load()
            sources = effective_sources(self.store.catalog, state)
            rows = _plan(records, sources)
            if fingerprint != _fingerprint(state, sources, pending, self.store.catalog):
                raise StaleSourceImport(
                    "Configuration or pending activation changed. Upload a new preview."
                )
            conflicts = frozenset(i for i, row in enumerate(rows) if row.status == "conflict")
            if keep_local != conflicts:
                raise SourceImportError(
                    "Every conflict requires an explicit keep-local decision, or cancel."
                )
            resolved = tuple(
                replace(row, status="kept_local") if row.status == "conflict" else row
                for row in rows
            )
            creates = sum(row.status == "create" for row in rows)
            if not creates:
                return SourceImportResult(resolved, 0)
            if state.schema_version < INSTALLED_SOURCES_SCHEMA_VERSION and not confirm_v8:
                raise SourceImportError(
                    "Explicit schema v8 compatibility acknowledgement is required."
                )
            candidate = self._candidate(state, rows, pending)
            try:
                self.store.save(candidate)
            except (OSError, ManagedStateError) as exc:
                # os.replace may already have committed the complete candidate. Never
                # retry blindly or claim rollback after a directory fsync failure.
                try:
                    observed = self.store.load_readonly()
                except OSError, ManagedStateError:
                    raise SourceImportWriteError(
                        "Import durability is uncertain. Readback failed; "
                        "upload a new preview before retrying."
                    ) from exc
                if observed == candidate:
                    return SourceImportResult(resolved, creates, recovered=True)
                if observed == state:
                    raise SourceImportWriteError(
                        "Import was not saved; the previous configuration is intact. "
                        "Upload a new preview to retry."
                    ) from exc
                raise SourceImportWriteError(
                    "Import outcome is uncertain. Configuration changed; "
                    "upload a new preview before retrying."
                ) from exc
            return SourceImportResult(resolved, creates)

    def _candidate(
        self,
        state: ManagedState,
        rows: tuple[SourceImportRow, ...],
        pending: ProvisioningIntent | None,
    ) -> ManagedState:
        additions = []
        for row in rows:
            if isinstance(row.incoming, UnsupportedPortableSource):
                raise UnsupportedSourceCapability("Unsupported sources cannot be installed")
            if row.status == "create":
                assert row.local_id is not None
                additions.append(
                    InstalledSource.from_definition(replace(row.incoming, id=row.local_id))
                )
        candidate = replace(
            state,
            schema_version=INSTALLED_SOURCES_SCHEMA_VERSION,
            sources=tuple(sorted((*state.sources, *additions), key=lambda item: item.id)),
        )
        serialize_managed_state(candidate, catalog=self.store.catalog)
        compile_engine_config(self.store.catalog, candidate)
        if pending is not None:
            sources = effective_sources(self.store.catalog, candidate)
            enabled = {source.id for source in sources if source.enabled}
            known = {source.id for source in sources}
            templates = effective_playlist_templates(
                self.store.catalog, candidate.templates, sources=sources
            )
            if (
                not set(pending.source_ids) <= known
                or not enabled.intersection(pending.source_ids)
                or pending.template_id not in {template.id for template in templates}
                or template_suspended_by_quarantine(
                    self.store.catalog, candidate, pending.template_id
                )
            ):
                raise SourceImportError(
                    "Pending activation references are not valid. "
                    "Resolve activation before importing."
                )
        return candidate
