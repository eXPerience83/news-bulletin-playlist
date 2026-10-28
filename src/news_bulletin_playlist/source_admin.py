"""Application boundary for custom-source creation and safe local lifecycle changes."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.engine_cycle_synchronization import EngineCycleSynchronization
from news_bulletin_playlist.managed_provisioning import (
    MANAGED_PROVISIONING_FILENAME,
    ProvisioningIntent,
    ProvisioningJournal,
)
from news_bulletin_playlist.managed_state import (
    ManagedPlaylist,
    ManagedSource,
    ManagedSourceOverride,
    ManagedState,
    ManagedStateStore,
    compile_engine_config,
    effective_sources,
    serialize_managed_state,
)
from news_bulletin_playlist.models import (
    EditorialScope,
    LanguageTag,
    ParserId,
    PublicReleaseMode,
    SourceDefinition,
    SourceId,
    SourceOrigin,
    SourceOriginKind,
    TemporalStrategy,
)
from news_bulletin_playlist.portable_sources import export_sources
from news_bulletin_playlist.source_inspection import (
    InspectionFailure,
    InspectionResponseReason,
    SourceInspection,
    SourceInspectionError,
    inspect_configured_source,
)
from news_bulletin_playlist.source_intake import (
    SourceIntakeError,
    SourceIntakeFailure,
    inspect_spotify_source,
    normalize_spotify_show_locator,
)
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetectorJournal,
    PublicEpisodeObservation,
    PublicShowClient,
    PublicShowHttpError,
    PublicShowTransportError,
    spotify_show_id,
)
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal
from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetrics

MAX_SOURCE_AUDIT_BATCH_SOURCES = 20
_SAFE_AUDIT_ARCHIVE_SOURCE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_GLOBAL_AUDIT_FAILURES = frozenset(
    {
        InspectionFailure.AUTHORIZATION,
        InspectionFailure.RATE_LIMITED,
        InspectionFailure.UNAVAILABLE,
    }
)


class _Synchronization(Protocol):
    def hold(self) -> AbstractContextManager[None]: ...


class _Auth(Protocol):
    def get_access_token(self, *, now: datetime | None = None) -> str: ...


class _BatchPublicClient:
    """Remember only public failures that justify suppressing later batch observations."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.global_failure = False

    def observe_episode(
        self,
        source_id: SourceId,
        spotify_show_id: str,
        *,
        observed_at: datetime,
    ) -> PublicEpisodeObservation:
        try:
            return cast(
                PublicEpisodeObservation,
                self.client.observe_episode(
                    source_id,
                    spotify_show_id,
                    observed_at=observed_at,
                ),
            )
        except PublicShowTransportError:
            self.global_failure = True
            raise
        except PublicShowHttpError as exc:
            if exc.status == 429 or exc.status >= 500:
                self.global_failure = True
            raise


class SourceMutationError(ValueError):
    """A requested source is absent or image-owned, not installation-owned."""


class SourceReferencedError(SourceMutationError):
    """A durable playlist or provisioning intent still owns this source."""


class SourceDisableError(SourceMutationError):
    """Disabling would leave an active or pending playlist without effective sources."""


def source_disable_blockers(
    source_id: SourceId,
    *,
    sources: tuple[SourceDefinition, ...],
    playlists: tuple[ManagedPlaylist, ...],
    provisioning_intent: ProvisioningIntent | None,
) -> tuple[str, ...]:
    """Use the same last-effective-source check for lifecycle writes and Admin controls."""
    remaining = {source.id for source in sources if source.enabled and source.id != source_id}
    blocked = tuple(
        f"{playlist.display_name} ({playlist.id})"
        for playlist in playlists
        if playlist.enabled
        and source_id in playlist.source_ids
        and not remaining.intersection(playlist.source_ids)
    )
    if (
        provisioning_intent is not None
        and source_id in provisioning_intent.source_ids
        and not remaining.intersection(provisioning_intent.source_ids)
    ):
        blocked += (f"Pending activation: {provisioning_intent.display_name}",)
    return blocked


class EngineCycleBusyError(SourceMutationError):
    """The engine cycle owns the destructive-maintenance gate."""


class SourceAuditBatchLimitError(ValueError):
    """The effective source set is too large for one bounded audit export."""


class SourceAuditBatchStateError(ValueError):
    """The effective source set cannot be represented safely in an audit archive."""


class NoSourcesToExportError(ValueError):
    """An empty installation cannot produce a source JSON v1 document."""


def export_source_configuration(
    store: ManagedStateStore,
    synchronization: _Synchronization,
    *,
    catalog: BuiltInCatalog = BUILTIN_CATALOG,
    source_id: str | None = None,
) -> bytes:
    """Export one coherent snapshot using only local configuration dependencies."""
    with synchronization.hold():
        state = store.load_readonly()
        sources = effective_sources(catalog, state)
        built_in_ids = {source.id for source in catalog.sources}
    if source_id is not None:
        sources = tuple(source for source in sources if source.id == source_id)
        if not sources:
            raise SourceMutationError("Configured source not found")
    if not sources:
        raise NoSourcesToExportError("No sources are available to export")
    return export_sources(sources, built_in_ids=built_in_ids)


@dataclass(frozen=True, slots=True)
class SourceAuditBatchItem:
    source_id: SourceId
    display_name: str
    ownership: str
    enabled: bool
    status: str
    report: SourceInspection | None = None
    failure: InspectionFailure | None = None
    response_reason: InspectionResponseReason | None = None
    not_attempted_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SourceAuditBatch:
    generated_at: datetime
    items: tuple[SourceAuditBatchItem, ...]
    public_observation_suppressed_after: SourceId | None = None


class SourceAdminService:
    """Keep provider I/O outside the configuration lock; serialize local mutations."""

    def __init__(
        self,
        store: ManagedStateStore,
        synchronization: _Synchronization,
        auth: _Auth,
        rate_limit: SpotifyRateLimitJournal,
        metrics: SpotifyRequestMetrics | None,
        *,
        catalog: BuiltInCatalog = BUILTIN_CATALOG,
        client_factory: Callable[[str], Any] | None = None,
        first_journal: SpotifyFirstJournal | None = None,
        detector_journal: PublicDetectorJournal | None = None,
        cycle_synchronization: EngineCycleSynchronization | None = None,
        provisioning_journal: ProvisioningJournal | None = None,
        public_client: Any | None = None,
    ) -> None:
        self.store = store
        self.synchronization = synchronization
        self.auth = auth
        self.rate_limit = rate_limit
        self.metrics = metrics
        self.catalog = catalog
        self.client_factory = client_factory
        self.first_journal = first_journal
        self.detector_journal = detector_journal
        self.cycle_synchronization = cycle_synchronization
        self.provisioning_journal = provisioning_journal or ProvisioningJournal(
            store.path.with_name(MANAGED_PROVISIONING_FILENAME)
        )
        self.public_client = public_client

    def snapshot(self) -> ManagedState:
        with self.synchronization.hold():
            state = self.store.load()
            effective_sources(self.catalog, state)
            return state

    def inspect(
        self, show_id: str, *, parser_id: ParserId, collection_filter_id: str | None
    ) -> SourceInspection:
        state = self.snapshot()
        return inspect_spotify_source(
            show_id,
            catalog=self.catalog,
            state=state,
            auth=self.auth,
            rate_limit=self.rate_limit,
            now=datetime.now(UTC),
            parser_id=parser_id,
            collection_filter_id=collection_filter_id,
            client_factory=self.client_factory,
            observe_public=False,
            metrics=self.metrics,
        )

    def audit(self, source_id: str) -> SourceInspection:
        """Inspect one current effective source without changing configured or runtime state."""
        state = self.snapshot()
        if source_id not in {str(source.id) for source in effective_sources(self.catalog, state)}:
            raise SourceMutationError("Configured source not found")
        return self._audit_from_snapshot(
            state,
            SourceId(source_id),
            now=datetime.now(UTC),
            observe_public=True,
            public_client=self.public_client,
        )

    def audit_all(
        self, *, on_progress: Callable[[int, int], None] | None = None
    ) -> SourceAuditBatch:
        """Audit a snapshot, optionally reporting completed/total to a job worker."""
        state = self.snapshot()
        sources = effective_sources(self.catalog, state)
        if len(sources) > MAX_SOURCE_AUDIT_BATCH_SOURCES:
            raise SourceAuditBatchLimitError(
                f"source audit batch exceeds {MAX_SOURCE_AUDIT_BATCH_SOURCES} sources"
            )
        unsafe = next(
            (
                source.id
                for source in sources
                if _SAFE_AUDIT_ARCHIVE_SOURCE_ID.fullmatch(str(source.id)) is None
            ),
            None,
        )
        if unsafe is not None:
            raise SourceAuditBatchStateError("effective source id is unsafe for archive export")

        generated_at = datetime.now(UTC)
        built_in_ids = {source.id for source in self.catalog.sources}
        items: list[SourceAuditBatchItem] = []
        stop_failure: InspectionFailure | None = None
        public_observation_enabled = True
        public_suppressed_after: SourceId | None = None
        if on_progress is not None:
            on_progress(0, len(sources))
        public_client = _BatchPublicClient(
            self.public_client
            if self.public_client is not None
            else PublicShowClient(metrics=self.metrics)
        )

        for source in sources:
            ownership = "built_in" if source.id in built_in_ids else "custom"
            if stop_failure is not None:
                items.append(
                    SourceAuditBatchItem(
                        source_id=source.id,
                        display_name=source.display_name,
                        ownership=ownership,
                        enabled=source.enabled,
                        status="not_attempted",
                        not_attempted_reason=f"batch_stopped_{stop_failure.value}",
                    )
                )
                if on_progress is not None:
                    on_progress(len(items), len(sources))
                continue
            try:
                report = self._audit_from_snapshot(
                    state,
                    source.id,
                    now=generated_at,
                    observe_public=public_observation_enabled,
                    public_client=public_client,
                )
            except SourceInspectionError as exc:
                items.append(
                    SourceAuditBatchItem(
                        source_id=source.id,
                        display_name=source.display_name,
                        ownership=ownership,
                        enabled=source.enabled,
                        status="failed",
                        failure=exc.failure,
                        response_reason=exc.response_reason,
                    )
                )
                if exc.failure in _GLOBAL_AUDIT_FAILURES:
                    stop_failure = exc.failure
                if on_progress is not None:
                    on_progress(len(items), len(sources))
                continue

            items.append(
                SourceAuditBatchItem(
                    source_id=source.id,
                    display_name=source.display_name,
                    ownership=ownership,
                    enabled=source.enabled,
                    status="succeeded",
                    report=report,
                )
            )
            if public_observation_enabled and public_client.global_failure:
                public_observation_enabled = False
                public_suppressed_after = source.id
            if on_progress is not None:
                on_progress(len(items), len(sources))

        return SourceAuditBatch(
            generated_at=generated_at,
            items=tuple(items),
            public_observation_suppressed_after=public_suppressed_after,
        )

    def _audit_from_snapshot(
        self,
        state: ManagedState,
        source_id: SourceId,
        *,
        now: datetime,
        observe_public: bool,
        public_client: Any | None,
    ) -> SourceInspection:
        return inspect_configured_source(
            source_id,
            catalog=self.catalog,
            state=state,
            auth=self.auth,
            rate_limit=self.rate_limit,
            now=now,
            client_factory=self.client_factory,
            public_client=public_client,
            observe_public=observe_public,
            metrics=self.metrics,
        )

    def validate_new_source(
        self,
        *,
        show_id: str,
        display_name: str,
        origin_kind: str,
        origin_code: str,
        language: str,
        editorial_scope: str,
        timezone: str,
        parser_id: str,
        collection_filter_id: str | None,
        temporal_strategy: str = "auto",
        public_release_mode: str = "instant",
    ) -> ManagedSource:
        canonical = normalize_spotify_show_locator(show_id)
        if canonical != show_id:
            raise ValueError("Spotify show ID must be canonical")
        try:
            source = ManagedSource(
                id=SourceId(f"custom_{show_id}"),
                spotify_show_id=show_id,
                display_name=display_name.strip(),
                origin=SourceOrigin(SourceOriginKind(origin_kind), origin_code),
                language=LanguageTag(language),
                editorial_scope=EditorialScope(editorial_scope),
                timezone=timezone,
                parser_id=ParserId(parser_id),
                collection_filter_id=collection_filter_id,
                enabled=True,
                temporal_strategy=TemporalStrategy(temporal_strategy),
                public_release_mode=PublicReleaseMode(public_release_mode),
            )
            serialize_managed_state(ManagedState(sources=(source,)))
        except ValueError as exc:
            raise ValueError("Source classification is invalid") from exc
        return source

    def create(self, source: ManagedSource) -> None:
        # Cheap local duplicate check before spending provider quota.
        self._check_duplicate(self.snapshot(), source)
        self.inspect(
            source.spotify_show_id,
            parser_id=source.parser_id,
            collection_filter_id=source.collection_filter_id,
        )
        with self.synchronization.hold():
            latest = self.store.load()
            self._check_duplicate(latest, source)
            self.store.save(replace(latest, sources=(*latest.sources, source)))

    def get_builtin_source(self, source_id: str) -> SourceDefinition:
        state = self.snapshot()
        if source_id not in {source.id for source in self.catalog.sources}:
            raise SourceMutationError("Built-in source not found")
        return next(
            source for source in effective_sources(self.catalog, state) if source.id == source_id
        )

    def update_builtin_public_release_mode(
        self, source_id: str, mode: str
    ) -> tuple[SourceDefinition, bool]:
        selected = PublicReleaseMode(mode)
        with self.synchronization.hold():
            state = self.store.load()
            base = next((source for source in self.catalog.sources if source.id == source_id), None)
            if base is None:
                raise SourceMutationError("Built-in source not found")
            overrides = tuple(
                item for item in state.source_overrides if item.source_id != source_id
            )
            # Choosing the image default removes the override, restoring image ownership.
            if selected is not base.public_release_mode:
                overrides += (ManagedSourceOverride(base.id, selected),)
            updated = replace(state, source_overrides=overrides)
            compile_engine_config(self.catalog, updated)
            self.store.save(updated)
            return replace(base, public_release_mode=selected), any(
                p.enabled for p in state.playlists
            )

    def get_custom_source(self, source_id: str) -> ManagedSource:
        state = self.snapshot()
        return self._custom_source(state, source_id)

    def update_metadata(
        self,
        source_id: str,
        *,
        display_name: str,
        origin_kind: str,
        origin_code: str,
        language: str,
        editorial_scope: str,
        temporal_strategy: str | None = None,
        public_release_mode: str | None = None,
    ) -> tuple[ManagedSource, bool]:
        with self.synchronization.hold():
            state = self.store.load()
            current = self._custom_source(state, source_id)
            try:
                updated = replace(
                    current,
                    display_name=display_name.strip(),
                    origin=SourceOrigin(SourceOriginKind(origin_kind), origin_code),
                    language=LanguageTag(language),
                    editorial_scope=EditorialScope(editorial_scope),
                    temporal_strategy=current.temporal_strategy
                    if temporal_strategy is None
                    else TemporalStrategy(temporal_strategy),
                    public_release_mode=current.public_release_mode
                    if public_release_mode is None
                    else PublicReleaseMode(public_release_mode),
                )
                serialize_managed_state(ManagedState(sources=(updated,)))
            except ValueError as exc:
                raise ValueError("Source metadata is invalid") from exc
            self._save_replacement(state, updated)
            return updated, any(playlist.enabled for playlist in state.playlists)

    def set_enabled(self, source_id: str, enabled: bool) -> tuple[ManagedSource, bool]:
        with self.synchronization.hold():
            state = self.store.load()
            current = self._custom_source(state, source_id)
            if not enabled:
                blocked = source_disable_blockers(
                    current.id,
                    sources=effective_sources(self.catalog, state),
                    playlists=state.playlists,
                    provisioning_intent=self.provisioning_journal.load(),
                )
                if blocked:
                    raise SourceDisableError(
                        "Cannot disable source; these playlists would have no enabled sources: "
                        + ", ".join(blocked)
                        + ". Add another enabled source or pause the active playlists first; "
                        "resolve any pending activation."
                    )
            updated = replace(current, enabled=enabled)
            self._save_replacement(state, updated)
            return updated, any(playlist.enabled for playlist in state.playlists)

    def deletion_references(self, source_id: str) -> tuple[ManagedSource, tuple[str, ...], bool]:
        """Read current references for confirmation; Delete checks them again under both locks."""
        with self.synchronization.hold():
            state = self.store.load()
            source = self._custom_source(state, source_id)
            used_by = tuple(
                playlist.display_name
                for playlist in state.playlists
                if source.id in playlist.source_ids
            ) + tuple(
                f"{template.display_name} (custom template)"
                for template in state.templates
                if source.id in template.default_source_ids
            )
            intent = self.provisioning_journal.load()
            pending = intent is not None and source.id in intent.source_ids
            if state.quarantined_templates:
                used_by += ("Unresolved custom playlist drafts",)
            return source, used_by, pending

    def delete(self, source_id: str) -> bool:
        """Remove the definition last, after source-local caches are cleared.

        A partial failure leaves the stable definition owning its ID. A retry is safe because
        both journal clear operations are idempotent and missing current state requires bootstrap.
        """
        _, used_by, pending = self.deletion_references(source_id)
        if used_by or pending:
            raise SourceReferencedError("Unassign this source before deletion")
        gate = self.cycle_synchronization
        if gate is None or self.first_journal is None or self.detector_journal is None:
            raise RuntimeError("Source deletion is unavailable")
        with gate.try_hold_maintenance() as acquired:
            if not acquired:
                raise EngineCycleBusyError("Engine cycle is active; retry deletion shortly")
            # Lock order is always cycle gate -> configuration lock, also in the cycle runner.
            with self.synchronization.hold():
                state = self.store.load()
                source = self._custom_source(state, source_id)
                intent = self.provisioning_journal.load()
                if (
                    any(source.id in playlist.source_ids for playlist in state.playlists)
                    or any(source.id in template.default_source_ids for template in state.templates)
                    or state.quarantined_templates
                    or (intent is not None and source.id in intent.source_ids)
                ):
                    raise SourceReferencedError("Unassign this source before deletion")
                next_state = replace(
                    state,
                    sources=tuple(item for item in state.sources if item.id != source.id),
                )
                compile_engine_config(self.catalog, next_state)
                self.first_journal.clear_source_runtime_state(source.id)
                self.detector_journal.clear_source_state(source.id)
                self.store.save(next_state)
                return any(playlist.enabled for playlist in state.playlists)

    def _custom_source(self, state: ManagedState, source_id: str) -> ManagedSource:
        source = next((item for item in state.sources if item.id == source_id), None)
        if source is None:
            raise SourceMutationError("Custom source is unavailable or not editable")
        return source

    def _save_replacement(self, state: ManagedState, updated: ManagedSource) -> None:
        next_state = replace(
            state,
            sources=tuple(updated if item.id == updated.id else item for item in state.sources),
        )
        compile_engine_config(self.catalog, next_state)
        self.store.save(next_state)

    def _check_duplicate(self, state: ManagedState, source: ManagedSource) -> None:
        sources = effective_sources(self.catalog, state)
        if any(
            item.id == source.id or spotify_show_id(item) == source.spotify_show_id
            for item in sources
        ):
            raise SourceIntakeError(SourceIntakeFailure.DUPLICATE_SHOW)
