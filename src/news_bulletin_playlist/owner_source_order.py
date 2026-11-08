"""Pure owner-owned source order projection for staged v11 catalog materialization.

This module deliberately does not change v11 storage, live Admin or Spotify.
Managed state serializes installed sources in canonical ID order, but historical
detection and runtime source order are semantic. A separate strict order vector
lets a future owner-owned authority reconstruct the original order exactly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedStateError,
    source_definition,
)
from news_bulletin_playlist.models import SourceDefinition, SourceId

_ORDER_FORMAT = "news-bulletin-playlist.owner-source-order"
_MAX_ORDER_BYTES = 64 * 1024
_MAX_OWNER_SOURCES = 512
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class OwnerSourceOrderError(ValueError):
    """Invalid source inventory/order evidence cannot silently reorder a runtime."""


@dataclass(frozen=True, slots=True)
class OwnerSourceOrder:
    ids: tuple[SourceId, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.ids, tuple)
            or len(self.ids) > _MAX_OWNER_SOURCES
            or len(self.ids) != len(set(self.ids))
            or any(
                not isinstance(item, str) or _SOURCE_ID.fullmatch(item) is None
                for item in self.ids
            )
        ):
            raise OwnerSourceOrderError("Owner source order is invalid or contains duplicate IDs")

    def serialize(self) -> bytes:
        """Canonical, bounded, provider-independent order evidence."""
        payload = (
            json.dumps(
                {
                    "format": _ORDER_FORMAT,
                    "version": 1,
                    "source_ids": list(self.ids),
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(payload) > _MAX_ORDER_BYTES:
            raise OwnerSourceOrderError("Owner source order exceeds its size limit")
        return payload

    @classmethod
    def parse(cls, payload: bytes) -> OwnerSourceOrder:
        if not isinstance(payload, bytes) or not 0 < len(payload) <= _MAX_ORDER_BYTES:
            raise OwnerSourceOrderError("Invalid owner source order length")

        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            output: dict[str, object] = {}
            for key, value in pairs:
                if key in output:
                    raise OwnerSourceOrderError("Duplicate owner source order key")
                output[key] = value
            return output

        try:
            raw: Any = json.loads(payload, object_pairs_hook=unique)
            if (
                not isinstance(raw, dict)
                or set(raw) != {"format", "version", "source_ids"}
                or raw["format"] != _ORDER_FORMAT
                or type(raw["version"]) is not int
                or raw["version"] != 1
                or not isinstance(raw["source_ids"], list)
            ):
                raise OwnerSourceOrderError("Unsupported owner source order document")
            result = cls(tuple(raw["source_ids"]))
            if result.serialize() != payload:
                raise OwnerSourceOrderError("Noncanonical owner source order")
            return result
        except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise OwnerSourceOrderError("Cannot validate owner source order") from exc

    def definitions(self, records: Iterable[InstalledSource]) -> tuple[SourceDefinition, ...]:
        """Build exact source-order runtime from installation-owned records only."""
        entries = tuple(records)
        by_id = {item.id: item for item in entries}
        if (
            len(by_id) != len(entries)
            or set(by_id) != set(self.ids)
            or any(not isinstance(item, InstalledSource) for item in entries)
        ):
            raise OwnerSourceOrderError("Owner source records do not match saved order")
        try:
            # The exporter/decoder is the authority for supported specialized
            # parsers, temporal behavior, language/country and filter IDs.
            definitions = tuple(
                source_definition(InstalledSource.from_definition(source_definition(by_id[id_])))
                for id_ in self.ids
            )
        except (ManagedStateError, ValueError, TypeError, KeyError) as exc:
            raise OwnerSourceOrderError("Unsupported owner source capability") from exc
        shows = [
            next(
                reference.external_id
                for reference in source.external_references
                if reference.system == "spotify" and reference.resource_type == "show"
            )
            for source in definitions
        ]
        if len(set(shows)) != len(shows):
            raise OwnerSourceOrderError("Duplicate Spotify show in owner source inventory")
        return definitions

    def after_records_change(
        self,
        before: Iterable[InstalledSource],
        after: Iterable[InstalledSource],
    ) -> OwnerSourceOrder:
        """Retain surviving order and append new IDs deterministically.

        Source deletion safety/reference checks belong to the state mutation
        service, not to this pure order helper. The original inventory MUST
        match the previously persisted order exactly.
        """
        self.definitions(before)
        updated = tuple(after)
        ids = [item.id for item in updated]
        if len(ids) != len(set(ids)):
            raise OwnerSourceOrderError("Duplicate updated source ID")
        surviving = tuple(item for item in self.ids if item in ids)
        added = tuple(sorted((item for item in ids if item not in self.ids), key=str))
        next_order = OwnerSourceOrder(surviving + added)
        next_order.definitions(updated)
        return next_order


def materialize_owner_sources(
    original_runtime_sources: tuple[SourceDefinition, ...],
) -> tuple[OwnerSourceOrder, tuple[InstalledSource, ...]]:
    """Pure lossless projection of image+owner sources to editable full records.

    Caller is responsible for the future authoritative state publication.
    No provider request, filesystem read/write or v11 change is performed here.
    """
    try:
        records = tuple(
            InstalledSource.from_definition(source) for source in original_runtime_sources
        )
        order = OwnerSourceOrder(tuple(source.id for source in original_runtime_sources))
        if order.definitions(records) != original_runtime_sources:
            raise OwnerSourceOrderError("Source conversion would change runtime definitions")
    except (ManagedStateError, ValueError, TypeError) as exc:
        raise OwnerSourceOrderError("Historical sources cannot be materialized losslessly") from exc
    return order, records
