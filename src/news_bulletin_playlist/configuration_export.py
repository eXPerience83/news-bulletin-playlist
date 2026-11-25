"""Single-snapshot portable configuration export; no provider or import side effects."""

from __future__ import annotations

import hashlib
import io
import struct
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from news_bulletin_playlist.catalog import PlaylistTemplate
from news_bulletin_playlist.models import (
    CountryCode,
    DurationPolicy,
    LanguageTag,
    OrderingPolicy,
    PlaylistId,
    SourceId,
)
from news_bulletin_playlist.owner_inventory_export import (
    MAX_OWNER_EXPORT_BYTES,
    build_owner_inventory,
    inspect_owner_inventory,
)
from news_bulletin_playlist.playlist_bundle import PlaylistBundle, inspect_playlist_bundle_v3
from news_bulletin_playlist.playlist_collection import (
    MAX_ARCHIVE_BYTES,
    check_classic_directory,
    inspect_collection,
    write_stored_entry,
)
from news_bulletin_playlist.portable_identity import portable_id
from news_bulletin_playlist.portable_playlist_metadata import exact_object, strict_json
from news_bulletin_playlist.portable_playlists import (
    PortableCoverLoader,
    Synchronization,
    canonical_json,
    collection_from_snapshot,
)
from news_bulletin_playlist.portable_sources import PortableSource
from news_bulletin_playlist.v11_managed_adapter import ForwardOnlyManagedStateStore
from news_bulletin_playlist.v11_operational_codec import parse_v11_operational

_FORMAT = "news-bulletin-playlist.configuration"
_OWNER = "owner-inventory-v1.json"
_COLLECTION = "playlists-v3-collection.zip"
_INDEX = "manifest.json"
_HASH = "manifest.sha256"
_MAX_INDEX_BYTES = 256 * 1024
MAX_CONFIGURATION_BYTES = MAX_OWNER_EXPORT_BYTES + MAX_ARCHIVE_BYTES + 2 * _MAX_INDEX_BYTES


class ConfigurationExportError(ValueError):
    """A complete portable configuration could not be exported or verified."""


@dataclass(frozen=True, slots=True)
class TemplateLink:
    playlist_uuid: str
    template_kind: str
    template_id: str


@dataclass(frozen=True, slots=True)
class ConfigurationExport:
    owner_document: bytes
    collection_document: bytes
    managed_links: tuple[TemplateLink, ...]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _descriptor(data: bytes) -> dict[str, object]:
    return {"size": len(data), "sha256": _sha(data)}


def _local_template(bundle: PlaylistBundle) -> PlaylistTemplate:
    """Recover the template projection of an independent local draft."""
    p = bundle.playlist
    return PlaylistTemplate(
        id=PlaylistId(p["original_id"]),
        display_name=p["display_name"],
        description=p["description"],
        countries=tuple(CountryCode(x) for x in p["countries"]),
        languages=tuple(LanguageTag(x) for x in p["languages"]),
        default_source_ids=tuple(SourceId(x) for x in p["source_ids"]),
        cover_id=p["cover"]["original_id"],
        retention_hours=p["retention_hours"],
        max_episodes=p["max_episodes"],
        ordering=OrderingPolicy.EDITION_AT_DESC,
        duration_policy=DurationPolicy(
            default_max_seconds=p["duration_policy"]["max_seconds"],
            default_min_seconds=p["duration_policy"]["min_seconds"],
            exceptions=bundle.exceptions,
        ),
    )


def _validate(export: ConfigurationExport) -> None:
    owner = inspect_owner_inventory(export.owner_document)
    members = inspect_collection(export.collection_document)
    sources = {
        item.id: item for item in owner.inventory.source_order.definitions(owner.inventory.sources)
    }
    templates = {str(t.id): t for t in owner.inventory.templates.records}
    covers = {item.cover_id: item.jpeg for item in owner.artwork}
    drafts: dict[str, PlaylistBundle] = {}
    managed: dict[str, PlaylistBundle] = {}
    original_ids: set[tuple[str, str]] = set()

    for member in members:
        bundle = inspect_playlist_bundle_v3(member.document)
        identity = (member.kind, bundle.playlist["original_id"])
        if identity in original_ids:
            raise ConfigurationExportError("Duplicate original playlist identity")
        original_ids.add(identity)
        for record in bundle.sources:
            if (
                not isinstance(record, PortableSource)
                or sources.get(record.source.id) != record.source
            ):
                raise ConfigurationExportError("Playlist source differs from owner inventory")
        cover_id = bundle.playlist["cover"]["original_id"]
        if covers.setdefault(cover_id, bundle.jpeg) != bundle.jpeg:
            raise ConfigurationExportError("Inconsistent playlist artwork")
        template = bundle.template
        if template is not None:
            if bundle.template_jpeg is None:
                raise ConfigurationExportError("Missing template artwork")
            if covers.setdefault(template.cover_id, bundle.template_jpeg) != bundle.template_jpeg:
                raise ConfigurationExportError("Inconsistent template artwork")
        if member.kind == "draft":
            if identity[1] in templates:
                raise ConfigurationExportError("Draft identity collides with an owner template")
            if template is not None and templates.get(str(template.id)) != template:
                raise ConfigurationExportError("Linked draft template differs from owner inventory")
            drafts[member.portable_id] = bundle
        else:
            managed[member.portable_id] = bundle

    if tuple(link.playlist_uuid for link in export.managed_links) != tuple(sorted(managed)):
        raise ConfigurationExportError("Managed template associations are incomplete or duplicated")
    for link in export.managed_links:
        if link.template_kind == "owner":
            expected = templates.get(link.template_id)
        elif link.template_kind == "draft":
            draft = drafts.get(link.template_id)
            expected = None if draft is None else draft.template or _local_template(draft)
        else:
            raise ConfigurationExportError("Unknown managed template association")
        if expected is None or managed[link.playlist_uuid].template != expected:
            raise ConfigurationExportError("Managed template association does not match its ZIP")


def _encode(export: ConfigurationExport) -> bytes:
    if (
        not 0 < len(export.owner_document) <= MAX_OWNER_EXPORT_BYTES
        or not 0 < len(export.collection_document) <= MAX_ARCHIVE_BYTES
    ):
        raise ConfigurationExportError("Configuration component exceeds size limit")
    manifest = canonical_json(
        {
            "format": _FORMAT,
            "version": 1,
            "files": {
                _OWNER: _descriptor(export.owner_document),
                _COLLECTION: _descriptor(export.collection_document),
            },
            "managed_template_links": [
                {
                    "playlist_uuid": link.playlist_uuid,
                    "template_kind": link.template_kind,
                    "template_id": link.template_id,
                }
                for link in export.managed_links
            ],
        }
    )
    if len(manifest) > _MAX_INDEX_BYTES:
        raise ConfigurationExportError("Configuration manifest exceeds size limit")
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED, allowZip64=False) as archive:
        for path, data in (
            (_INDEX, manifest),
            (_HASH, (_sha(manifest) + "\n").encode("ascii")),
            (_OWNER, export.owner_document),
            (_COLLECTION, export.collection_document),
        ):
            write_stored_entry(archive, path, data)
    return output.getvalue()


def export_configuration(data_dir: Path, synchronization: Synchronization) -> bytes:
    """Read exactly one authority under the configuration lock; never initialize it."""
    try:
        with synchronization.hold():
            store = ForwardOnlyManagedStateStore(data_dir, readonly=True)
            owned = parse_v11_operational(store._expected_bytes)
            state = owned.managed_overlay
            covers = PortableCoverLoader(data_dir)
            owner = build_owner_inventory(owned, covers)
            collection = collection_from_snapshot(state, store, covers)
            drafts = {draft.id: draft.portable_id for draft in state.templates}
            links = []
            for playlist in state.playlists:
                identity = portable_id(playlist.portable_id)
                if identity is None:
                    raise ConfigurationExportError("Missing managed portable identity")
                if playlist.template_id in drafts:
                    draft_uuid = portable_id(drafts[playlist.template_id])
                    if draft_uuid is None:
                        raise ConfigurationExportError("Missing draft portable identity")
                    links.append(TemplateLink(identity, "draft", draft_uuid))
                else:
                    links.append(TemplateLink(identity, "owner", str(playlist.template_id)))
            result = ConfigurationExport(
                owner, collection, tuple(sorted(links, key=lambda link: link.playlist_uuid))
            )
            _validate(result)
            return _encode(result)
    except ConfigurationExportError:
        raise
    except (ValueError, OSError, TypeError, KeyError, RecursionError) as exc:
        raise ConfigurationExportError("Configuration could not be exported safely") from exc


def inspect_configuration(document: bytes) -> ConfigurationExport:
    """Validate the bounded canonical envelope and cross-component closure without writes."""
    if not isinstance(document, bytes) or not 22 < len(document) <= MAX_CONFIGURATION_BYTES:
        raise ConfigurationExportError("Invalid configuration archive size")
    try:
        eocd = struct.unpack("<4s4H2IH", document[-22:])
        if (
            eocd[:5] != (b"PK\x05\x06", 0, 0, 4, 4)
            or eocd[7] != 0
            or not 4 * 46 <= eocd[5] <= 4 * 96
            or eocd[5] + eocd[6] != len(document) - 22
        ):
            raise ConfigurationExportError("Invalid configuration ZIP envelope")
        check_classic_directory(document, eocd[6], 4)
        limits = {
            _INDEX: _MAX_INDEX_BYTES,
            _HASH: 65,
            _OWNER: MAX_OWNER_EXPORT_BYTES,
            _COLLECTION: MAX_ARCHIVE_BYTES,
        }
        with ZipFile(io.BytesIO(document), allowZip64=False) as archive:
            infos = archive.infolist()
            if [info.filename for info in infos] != list(limits):
                raise ConfigurationExportError("Invalid configuration ZIP entries")
            for info in infos:
                if (
                    info.flag_bits
                    or info.compress_type != ZIP_STORED
                    or info.compress_size != info.file_size
                    or not 0 < info.file_size <= limits[info.filename]
                ):
                    raise ConfigurationExportError("Invalid configuration ZIP entry")
            manifest = archive.read(_INDEX)
            if archive.read(_HASH) != (_sha(manifest) + "\n").encode("ascii"):
                raise ConfigurationExportError("Configuration manifest digest mismatch")
            index = exact_object(
                strict_json(manifest), {"format", "version", "files", "managed_template_links"}
            )
            rows = index["managed_template_links"]
            if (
                index["format"] != _FORMAT
                or type(index["version"]) is not int
                or index["version"] != 1
                or not isinstance(rows, list)
                or len(rows) > 256
            ):
                raise ConfigurationExportError("Unsupported configuration manifest")
            links = []
            for row in rows:
                item = exact_object(row, {"playlist_uuid", "template_kind", "template_id"})
                identity = portable_id(item["playlist_uuid"])
                if (
                    identity is None
                    or item["template_kind"] not in ("owner", "draft")
                    or not isinstance(item["template_id"], str)
                    or not 0 < len(item["template_id"]) <= 128
                ):
                    raise ConfigurationExportError("Invalid managed template association")
                links.append(TemplateLink(identity, item["template_kind"], item["template_id"]))
            owner = archive.read(_OWNER)
            collection = archive.read(_COLLECTION)
            if index["files"] != {_OWNER: _descriptor(owner), _COLLECTION: _descriptor(collection)}:
                raise ConfigurationExportError("Configuration component digest mismatch")
        result = ConfigurationExport(owner, collection, tuple(links))
        if _encode(result) != document:
            raise ConfigurationExportError("Noncanonical configuration archive")
        _validate(result)
        return result
    except ConfigurationExportError:
        raise
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, BadZipFile) as exc:
        raise ConfigurationExportError("Invalid portable configuration archive") from exc
