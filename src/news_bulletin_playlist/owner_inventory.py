"""Source/template ownership inventory serialized inside the durable v11 authority.

The immutable historical snapshot remains provenance evidence. This is the
canonical installed-owner representation that can later become the active Admin
catalog. Until a separate runtime switch is accepted, no live config edits or
provider writes are performed against these records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from news_bulletin_playlist.legacy_catalog_snapshot import FrozenLegacyCatalogSnapshot
from news_bulletin_playlist.legacy_materialization import LegacyProvenance
from news_bulletin_playlist.managed_state import (
    InstalledSource,
    ManagedStateError,
    source_definition,
)
from news_bulletin_playlist.owner_source_order import (
    OwnerSourceOrder,
    OwnerSourceOrderError,
)
from news_bulletin_playlist.owner_template_records import (
    OwnerTemplateError,
    OwnerTemplateSet,
    materialize_owner_templates,
)
from news_bulletin_playlist.portable_playlist_metadata import exact_object
from news_bulletin_playlist.portable_sources import (
    PortableSourceError,
    export_sources,
    parse_sources,
)


class OwnerInventoryError(ValueError):
    """Installed v11 owner inventory cannot be checked against exact migration evidence."""


def _compact(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class OwnerInventory:
    source_order: OwnerSourceOrder
    sources: tuple[InstalledSource, ...]
    templates: OwnerTemplateSet

    @classmethod
    def from_snapshot(cls, snapshot: FrozenLegacyCatalogSnapshot) -> OwnerInventory:
        """Freeze *all* old sources, including customs and disabled assignments."""
        if snapshot.source_order is None:
            raise OwnerInventoryError("Cannot own sources without verified original order")
        by_id = {item.installed.id: item.installed for item in snapshot.sources}
        try:
            order = OwnerSourceOrder(snapshot.source_order)
            template_records = tuple(
                template.definition
                for template in snapshot.templates
                if template.provenance is LegacyProvenance.IMAGE_PRESET
            )
            templates = materialize_owner_templates(template_records, sources=order.ids)
            result = cls(
                source_order=order,
                sources=tuple(sorted(by_id.values(), key=lambda item: item.id)),
                templates=templates,
            )
            result.validate_against(snapshot)
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise OwnerInventoryError("Cannot materialize historical owner inventory") from exc

    def to_document(self) -> dict[str, object]:
        """Strict, standalone nested payload; no reference to a newer image."""
        try:
            self.templates.validate_sources(self.source_order.ids)
            canonical_sources = self.source_order.definitions(self.sources)
            return {
                "source_order": json.loads(self.source_order.serialize()),
                "installed_sources": json.loads(export_sources(canonical_sources)),
                "templates": json.loads(self.templates.serialize()),
            }
        except (ValueError, TypeError, UnicodeError, KeyError) as exc:
            raise OwnerInventoryError("Owner inventory cannot be serialized") from exc

    @classmethod
    def parse(cls, document: object) -> OwnerInventory:
        """Validate all independent codecs and exact membership, fail closed."""
        try:
            obj = exact_object(
                document, {"source_order", "installed_sources", "templates"}
            )
            order = OwnerSourceOrder.parse(_compact(obj["source_order"]))
            parsed_sources = parse_sources(_compact(obj["installed_sources"]))
            records = tuple(
                InstalledSource.from_definition(item.source) for item in parsed_sources
            )
            templates = OwnerTemplateSet.parse(_compact(obj["templates"]))
            result = cls(
                source_order=order,
                sources=tuple(sorted(records, key=lambda item: item.id)),
                templates=templates,
            )
            if result.to_document() != obj:
                raise OwnerInventoryError("Noncanonical owner inventory members")
            return result
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise OwnerInventoryError("Invalid installed owner inventory") from exc

    def validate_against(self, snapshot: FrozenLegacyCatalogSnapshot) -> None:
        """For this pre-activation stage, require exact historical equivalence.

        Future live owner mutations will need an explicit versioned authority
        transition, not a hidden exemption in this provenance check.
        """
        if snapshot.source_order is None or self.source_order.ids != snapshot.source_order:
            raise OwnerInventoryError("Owner inventory changed original source order")
        by_id = {item.installed.id: item.installed for item in snapshot.sources}
        if len(by_id) != len(snapshot.sources):
            raise OwnerInventoryError("Duplicate historical source identity")
        expected = tuple(source_definition(by_id[id_]) for id_ in snapshot.source_order)
        try:
            actual = self.source_order.definitions(self.sources)
            self.templates.validate_sources(self.source_order.ids)
        except (OwnerSourceOrderError, OwnerTemplateError, ManagedStateError) as exc:
            raise OwnerInventoryError("Incomplete installed owner source/template closure") from exc
        if actual != expected:
            raise OwnerInventoryError("Owner sources differ from historical capability metadata")
        original_templates = tuple(
            item.definition
            for item in snapshot.templates
            if item.provenance is LegacyProvenance.IMAGE_PRESET
        )
        if self.templates.records != original_templates:
            raise OwnerInventoryError("Owner templates differ from historical policies")
