"""Application boundary for custom-source creation and safe local lifecycle changes."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.engine_cycle_synchronization import EngineCycleSynchronization
from news_bulletin_playlist.managed_provisioning import (
    MANAGED_PROVISIONING_FILENAME,
    ProvisioningJournal,
)
from news_bulletin_playlist.managed_state import (
    ManagedSource,
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
    SourceId,
    SourceOrigin,
    SourceOriginKind,
)
from news_bulletin_playlist.source_inspection import (
    SourceInspection,
    inspect_configured_source,
)
from news_bulletin_playlist.source_intake import (
    SourceIntakeError,
    SourceIntakeFailure,
    inspect_spotify_source,
    normalize_spotify_show_locator,
)
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import PublicDetectorJournal, spotify_show_id
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal
from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetrics


class _Synchronization(Protocol):
    def hold(self) -> AbstractContextManager[None]: ...


class _Auth(Protocol):
    def get_access_token(self, *, now: datetime | None = None) -> str: ...


class SourceMutationError(ValueError):
    """A requested source is absent or image-owned, not installation-owned."""


class SourceReferencedError(SourceMutationError):
    """A durable playlist or provisioning intent still owns this source."""


class EngineCycleBusyError(SourceMutationError):
    """The engine cycle owns the destructive-maintenance gate."""


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
        return inspect_configured_source(
            SourceId(source_id),
            catalog=self.catalog,
            state=state,
            auth=self.auth,
            rate_limit=self.rate_limit,
            now=datetime.now(UTC),
            client_factory=self.client_factory,
            public_client=self.public_client,
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
            )
            intent = self.provisioning_journal.load()
            pending = intent is not None and source.id in intent.source_ids
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
                if any(source.id in playlist.source_ids for playlist in state.playlists) or (
                    intent is not None and source.id in intent.source_ids
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
