"""Source/template ownership inventory serialized inside the durable v11 authority.

The immutable historical snapshot remains provenance evidence. This is the
canonical installed-owner representation that can later become the active Admin
catalog. Until a separate runtime switch is accepted, no live config edits or
provider writes are performed against these records.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from news_bulletin_playlist.custom_cover_assets import is_uploaded_cover_id
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
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.owner_template_records import (
    OwnerTemplateError,
    OwnerTemplateSet,
    materialize_owner_templates,
)
from news_bulletin_playlist.portable_playlist_metadata import exact_object, validate_attribution
from news_bulletin_playlist.portable_sources import export_sources, parse_sources


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
    # Optional v11 owner JPEG credit records. Older canonical inventories have
    # no field and remain byte-compatible until the first explicit revision.
    cover_attributions: tuple[tuple[str, str], ...] = ()
    # Retired IDs remain reserved permanently so a crash between the v11 CAS
    # and SQLite cleanup cannot expose stale episodes to a new installation.
    retired_source_ids: tuple[SourceId, ...] = ()

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
            result: dict[str, object] = {
                "source_order": json.loads(self.source_order.serialize()),
                # Split the complete installation into bounded source v1
                # capability documents. A valid legacy root can have more
                # than 100 sources even though one portable v1 file cannot.
                "installed_sources": [
                    json.loads(export_sources(canonical_sources[i : i + 100]))
                    for i in range(0, len(canonical_sources), 100)
                ],
                "templates": json.loads(self.templates.serialize()),
            }
            if self.retired_source_ids:
                retired = self.retired_source_ids
                if (
                    len(retired) > 2048
                    or tuple(sorted(retired)) != retired
                    or len(set(retired)) != len(retired)
                    or set(retired).intersection(self.source_order.ids)
                    or any(
                        not isinstance(item, str)
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item) is None
                        for item in retired
                    )
                ):
                    raise OwnerInventoryError("Invalid or reused retired source identity")
                result["retired_source_ids"] = list(retired)
            if self.cover_attributions:
                if len(self.cover_attributions) > 256:
                    raise OwnerInventoryError("Too many owner cover attribution records")
                seen: set[str] = set()
                entries: list[dict[str, object]] = []
                for cover_id, encoded in self.cover_attributions:
                    if (
                        not isinstance(cover_id, str)
                        or not is_uploaded_cover_id(cover_id)
                        or cover_id in seen
                        or not isinstance(encoded, str)
                    ):
                        raise OwnerInventoryError("Invalid or duplicate owner cover attribution")
                    seen.add(cover_id)
                    attribution = validate_attribution(json.loads(encoded))
                    if _compact(attribution).decode("utf-8") != encoded:
                        raise OwnerInventoryError("Noncanonical owner cover attribution")
                    entries.append({"cover_id": cover_id, "attribution": attribution})
                if [row["cover_id"] for row in entries] != sorted(seen):
                    raise OwnerInventoryError("Owner cover attribution order is not canonical")
                result["cover_attributions"] = entries
            return result
        except (ValueError, TypeError, UnicodeError, KeyError) as exc:
            raise OwnerInventoryError("Owner inventory cannot be serialized") from exc

    @classmethod
    def parse(cls, document: object) -> OwnerInventory:
        """Validate all independent codecs and exact membership, fail closed."""
        try:
            if not isinstance(document, dict):
                raise OwnerInventoryError("Invalid owner inventory document")
            keys = {"source_order", "installed_sources", "templates"}
            if "cover_attributions" in document:
                keys.add("cover_attributions")
            if "retired_source_ids" in document:
                keys.add("retired_source_ids")
            obj = exact_object(document, keys)
            order = OwnerSourceOrder.parse(_compact(obj["source_order"]))
            chunks = obj["installed_sources"]
            if not isinstance(chunks, list) or len(chunks) > 6:
                raise OwnerInventoryError("Invalid bounded owner source chunks")
            records = tuple(
                InstalledSource.from_definition(item.source)
                for chunk in chunks
                for item in parse_sources(_compact(chunk))
            )
            templates = OwnerTemplateSet.parse(_compact(obj["templates"]))
            attribution_records = obj.get("cover_attributions", [])
            if not isinstance(attribution_records, list) or len(attribution_records) > 256:
                raise OwnerInventoryError("Invalid owner cover attribution inventory")
            attributions = tuple(
                (
                    exact_object(item, {"cover_id", "attribution"})["cover_id"],
                    _compact(validate_attribution(item["attribution"])).decode("utf-8"),
                )
                for item in attribution_records
            )
            retired = obj.get("retired_source_ids", [])
            if not isinstance(retired, list):
                raise OwnerInventoryError("Invalid retired source identity list")
            result = cls(
                source_order=order,
                sources=tuple(sorted(records, key=lambda item: item.id)),
                templates=templates,
                cover_attributions=attributions,
                retired_source_ids=tuple(retired),
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
        if self.retired_source_ids:
            raise OwnerInventoryError("Initial owner inventory cannot retire historical sources")
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
