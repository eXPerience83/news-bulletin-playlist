"""Read-only export of complete owner sources/templates and template artwork."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from news_bulletin_playlist.custom_cover_assets import (
    MAX_JPEG_BYTES,
    is_uploaded_cover_id,
    validate_canonical_jpeg,
)
from news_bulletin_playlist.managed_state import InstalledSource
from news_bulletin_playlist.owner_inventory import OwnerInventory
from news_bulletin_playlist.portable_playlist_metadata import (
    exact_object,
    strict_json,
    validate_attribution,
)
from news_bulletin_playlist.portable_playlists import PortableCoverLoader, Synchronization
from news_bulletin_playlist.v11_operational_codec import V11OperationalState
from news_bulletin_playlist.v11_operational_storage import V11OperationalStore

_FORMAT = "news-bulletin-playlist.owner-inventory-export"
MAX_OWNER_EXPORT_BYTES = 96 * 1024 * 1024
_MAX_ARTWORK = 256
_MAX_BASE64_BYTES = 4 * ((MAX_JPEG_BYTES + 2) // 3)


class OwnerExportError(ValueError):
    """A complete portable owner inventory cannot be produced or verified."""


@dataclass(frozen=True, slots=True)
class OwnerArtwork:
    cover_id: str
    jpeg: bytes
    attribution: dict[str, object]


@dataclass(frozen=True, slots=True)
class OwnerInventoryExport:
    inventory: OwnerInventory
    artwork: tuple[OwnerArtwork, ...]


def _canonical(value: object) -> bytes:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return (encoded + "\n").encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cover_ids(inventory: OwnerInventory) -> tuple[str, ...]:
    return tuple(sorted({template.cover_id for template in inventory.templates.records}))


def _validate_artwork(item: OwnerArtwork) -> None:
    validate_canonical_jpeg(item.jpeg)
    if is_uploaded_cover_id(item.cover_id) and item.cover_id != f"uploaded_{_sha(item.jpeg)}":
        raise OwnerExportError("Uploaded cover identity does not match its bytes")
    validate_attribution(item.attribution)


def _encode(export: OwnerInventoryExport) -> bytes:
    if tuple(item.cover_id for item in export.artwork) != _cover_ids(export.inventory):
        raise OwnerExportError("Owner template artwork closure is incomplete")
    if len(export.artwork) > _MAX_ARTWORK:
        raise OwnerExportError("Too many owner template covers")
    artwork = []
    for item in export.artwork:
        _validate_artwork(item)
        artwork.append(
            {
                "cover_id": item.cover_id,
                "size": len(item.jpeg),
                "sha256": _sha(item.jpeg),
                "jpeg_base64": base64.b64encode(item.jpeg).decode("ascii"),
                "attribution": item.attribution,
            }
        )
    payload = {"inventory": export.inventory.to_document(), "artwork": artwork}
    document = _canonical(
        {"format": _FORMAT, "version": 1, "payload": payload, "sha256": _sha(_canonical(payload))}
    )
    if len(document) > MAX_OWNER_EXPORT_BYTES:
        raise OwnerExportError("Owner inventory export exceeds size limit")
    return document


def build_owner_inventory(owned: V11OperationalState, covers: PortableCoverLoader) -> bytes:
    """Build from one validated authority while its caller holds the configuration lock."""
    if not owned.owner_sources_complete:
        raise OwnerExportError("Historical split-mode v11 requires explicit promotion")
    inventory = owned.owner_inventory
    # Fold legacy public-release overrides into the portable effective definitions.
    # Do not mutate the authority or change the semantic source order.
    if tuple(source.id for source in owned.runtime.sources) != inventory.source_order.ids:
        raise OwnerExportError("Owner source order differs from runtime")
    try:
        inventory = replace(
            inventory,
            sources=tuple(
                sorted(
                    (InstalledSource.from_definition(source) for source in owned.runtime.sources),
                    key=lambda source: source.id,
                )
            ),
        )
        credits = dict(inventory.cover_attributions)
        artwork = []
        for cover_id in _cover_ids(inventory):
            jpeg, attribution = covers.load(cover_id)
            if cover_id in credits:
                attribution = validate_attribution(strict_json(credits[cover_id].encode("utf-8")))
            artwork.append(OwnerArtwork(cover_id, jpeg, attribution))
        return _encode(OwnerInventoryExport(inventory, tuple(artwork)))
    except OwnerExportError:
        raise
    except (ValueError, OSError, TypeError) as exc:
        raise OwnerExportError("Owner template or artwork cannot be exported safely") from exc


def export_owner_inventory(data_dir: Path, synchronization: Synchronization) -> bytes:
    """Read the v11 authority once, with no initialization or provider access."""
    try:
        with synchronization.hold():
            owned = V11OperationalStore(data_dir).load()
            return build_owner_inventory(owned, PortableCoverLoader(data_dir))
    except OwnerExportError:
        raise
    except (ValueError, OSError) as exc:
        raise OwnerExportError("Owner inventory authority is unavailable") from exc


def inspect_owner_inventory(document: bytes) -> OwnerInventoryExport:
    """Validate an inert canonical export; never extract, install or write."""
    if not isinstance(document, bytes) or not 0 < len(document) <= MAX_OWNER_EXPORT_BYTES:
        raise OwnerExportError("Invalid owner inventory export size")
    try:
        root = exact_object(strict_json(document), {"format", "version", "payload", "sha256"})
        if root["format"] != _FORMAT or type(root["version"]) is not int or root["version"] != 1:
            raise OwnerExportError("Unsupported owner inventory export")
        payload = exact_object(root["payload"], {"inventory", "artwork"})
        if root["sha256"] != _sha(_canonical(payload)):
            raise OwnerExportError("Owner inventory export digest mismatch")
        inventory = OwnerInventory.parse(payload["inventory"])
        rows = payload["artwork"]
        expected = _cover_ids(inventory)
        if not isinstance(rows, list) or len(rows) != len(expected) or len(rows) > _MAX_ARTWORK:
            raise OwnerExportError("Invalid owner template artwork count")
        artwork = []
        credits = dict(inventory.cover_attributions)
        for cover_id, row in zip(expected, rows, strict=True):
            item = exact_object(row, {"cover_id", "size", "sha256", "jpeg_base64", "attribution"})
            encoded = item["jpeg_base64"]
            if (
                item["cover_id"] != cover_id
                or not isinstance(encoded, str)
                or not 0 < len(encoded) <= _MAX_BASE64_BYTES
                or type(item["size"]) is not int
                or not 0 < item["size"] <= MAX_JPEG_BYTES
            ):
                raise OwnerExportError("Invalid owner template artwork")
            jpeg = base64.b64decode(encoded, validate=True)
            if len(jpeg) != item["size"] or _sha(jpeg) != item["sha256"]:
                raise OwnerExportError("Owner template artwork digest mismatch")
            attribution = validate_attribution(item["attribution"])
            if cover_id in credits and _canonical(attribution).decode("utf-8") != credits[cover_id]:
                raise OwnerExportError("Owner template artwork attribution mismatch")
            artwork.append(OwnerArtwork(cover_id, jpeg, attribution))
        result = OwnerInventoryExport(inventory, tuple(artwork))
        if _encode(result) != document:
            raise OwnerExportError("Noncanonical owner inventory export")
        return result
    except OwnerExportError:
        raise
    except (ValueError, TypeError, KeyError, OSError, RecursionError) as exc:
        raise OwnerExportError("Invalid owner inventory export") from exc
