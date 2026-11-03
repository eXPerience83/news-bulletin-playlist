"""Reusable offline source-import and unsupported-preview test fixtures."""

from __future__ import annotations

import json
from dataclasses import replace

from _managed_fixtures import _source

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_provisioning import (
    ProvisioningIntent,
    ProvisioningJournal,
    ProvisioningState,
)
from news_bulletin_playlist.managed_state import ManagedStateStore, source_definition
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.portable_sources import export_sources
from news_bulletin_playlist.source_import import SourceImportService


def _service(tmp_path, *, catalog=BUILTIN_CATALOG):
    store = ManagedStateStore(tmp_path / "managed-state.json", catalog=catalog)
    return SourceImportService(
        store, ConfigurationSynchronization(), ProvisioningJournal(tmp_path / "pending.json")
    )


def _document(**changes):
    return export_sources((replace(source_definition(_source()), **changes),))


def _apply(service, document, *, keep_local=frozenset(), confirm_v8=True):
    preview = service.preview(document)
    return service.apply(
        document, preview.fingerprint, keep_local=keep_local, confirm_v8=confirm_v8
    )


def _pending(service, source_ids=("ser",)):
    intent = ProvisioningIntent(
        ProvisioningState.REQUEST_STARTED,
        BUILTIN_CATALOG.playlists[0].id,
        "Pending",
        "Keep description",
        "spain_spanish_news",
        source_ids,
        retention_hours=48,
        max_episodes=50,
        max_duration_seconds=900,
    )
    service.provisioning.save(intent)
    return intent


def _payload():
    return json.loads(export_sources((BUILTIN_CATALOG.sources[0],)))


def _bytes(payload):
    return json.dumps(payload).encode("utf-8")


def _mixed_document():
    records = [
        json.loads(export_sources((source_definition(_source("B" * 22)),)))["sources"][0],
        json.loads(
            export_sources((replace(BUILTIN_CATALOG.sources[0], id=SourceId("foreign_ser")),))
        )["sources"][0],
        json.loads(export_sources((source_definition(_source()),)))["sources"][0],
        json.loads(export_sources((replace(BUILTIN_CATALOG.sources[1], display_name="Conflict"),)))[
            "sources"
        ][0],
        json.loads(export_sources((source_definition(_source("C" * 22)),)))["sources"][0],
    ]
    records[-1]["processing"]["parser"]["id"] = "unknown"
    payload = _payload()
    payload["sources"] = records
    return _bytes(payload)
