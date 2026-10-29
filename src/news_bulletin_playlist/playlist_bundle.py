"""Strict portable playlist ZIP v1 inspection. Never extracts, installs or executes."""

from __future__ import annotations

import hashlib
import io
import re
import stat
import struct
from dataclasses import dataclass
from typing import Any
from zipfile import BadZipFile, ZipFile

from news_bulletin_playlist.custom_cover_assets import MAX_JPEG_BYTES, validate_canonical_jpeg
from news_bulletin_playlist.models import DurationPolicy, DurationPolicyException
from news_bulletin_playlist.portable_identity import portable_id
from news_bulletin_playlist.portable_playlist_metadata import (
    exact_object,
    parse_exception,
    strict_json,
    validate_attribution,
)
from news_bulletin_playlist.portable_playlists import (
    MAX_MANIFEST_BYTES,
    MAX_PLAYLIST_ZIP_BYTES,
    canonical_json,
)
from news_bulletin_playlist.portable_sources import (
    MAX_SOURCE_DOCUMENT_BYTES,
    PortableSource,
    UnsupportedPortableSource,
    inspect_sources,
)


class PlaylistBundleError(ValueError):
    """Sanitized, bounded package validation error."""


@dataclass(frozen=True, slots=True)
class PlaylistBundle:
    playlist: dict[str, Any]
    sources: tuple[PortableSource | UnsupportedPortableSource, ...]
    source_document: bytes
    jpeg: bytes
    exceptions: tuple[DurationPolicyException, ...]
    metadata: str


def _id(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None
    ):
        raise PlaylistBundleError("Invalid logical identity")
    return value


def _integer(value: object, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise PlaylistBundleError("Invalid portable policy integer")
    return value


def _tags(value: object, pattern: str) -> None:
    if (
        not isinstance(value, list)
        or len(value) > 100
        or any(not isinstance(x, str) or re.fullmatch(pattern, x) is None for x in value)
        or len(set(value)) != len(value)
    ):
        raise PlaylistBundleError("Invalid portable classification")


def _digest(data: bytes) -> dict[str, object]:
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _files(document: bytes) -> dict[str, bytes]:
    if not isinstance(document, bytes) or not 22 < len(document) <= MAX_PLAYLIST_ZIP_BYTES:
        raise PlaylistBundleError("Playlist ZIP must be at most 1 MiB")
    # Require the exact non-ZIP64, single-disk, comment-free envelope produced by v1.
    eocd = struct.unpack("<4s4H2IH", document[-22:])
    if (
        eocd[:5] != (b"PK\x05\x06", 0, 0, 4, 4)
        or eocd[7] != 0
        or eocd[5] + eocd[6] != len(document) - 22
    ):
        raise PlaylistBundleError("Unsupported ZIP envelope or entry count")
    output = {}
    with ZipFile(io.BytesIO(document), "r", allowZip64=False) as archive:
        entries = archive.infolist()
        if len(entries) != 4 or archive.comment:
            raise PlaylistBundleError("Expected exactly four ZIP entries")
        offset = 0
        total = 0
        for entry in entries:
            name = entry.filename
            limit = (
                MAX_MANIFEST_BYTES
                if name == "manifest.json"
                else 65
                if name == "manifest.sha256"
                else MAX_SOURCE_DOCUMENT_BYTES
                if name == "sources.json"
                else MAX_JPEG_BYTES
                if re.fullmatch(r"covers/[0-9a-f]{64}\.jpg", name)
                else 0
            )
            if (
                name in output
                or not limit
                or entry.extra
                or entry.comment
                or entry.flag_bits
                or entry.compress_type != 0
                or entry.create_system != 3
                or entry.extract_version != 20
                or entry.create_version != 20
                or entry.volume
                or entry.reserved
                or entry.external_attr != (stat.S_IFREG | 0o600) << 16
                or entry.internal_attr
                or entry.header_offset != offset
                or not 0 < entry.file_size <= limit
                or entry.compress_size != entry.file_size
            ):
                raise PlaylistBundleError("Unsafe ZIP entry, metadata or size")
            if entry.date_time != (1980, 1, 1, 0, 0, 0):
                raise PlaylistBundleError("Unsupported ZIP timestamp metadata")
            # Cross-check local headers, no prefix/gaps/hidden members/data descriptors.
            header = struct.unpack_from("<4s5H3I2H", document, offset)
            if (
                header[0] != b"PK\x03\x04"
                or header[1] != 20
                or header[2] != 0
                or header[3] != 0
                or header[4:6] != (0, 33)
                or header[6:9] != (entry.CRC, entry.compress_size, entry.file_size)
                or header[10] != 0
                or document[offset + 30 : offset + 30 + header[9]] != name.encode("ascii")
            ):
                raise PlaylistBundleError("Inconsistent ZIP local header")
            offset += 30 + header[9] + entry.file_size
            total += entry.file_size
            if total > MAX_PLAYLIST_ZIP_BYTES or offset > eocd[6]:
                raise PlaylistBundleError("ZIP expansion exceeds limits")
            with archive.open(entry) as stream:
                data = stream.read(limit + 1)
                if len(data) != entry.file_size or stream.read(1):
                    raise PlaylistBundleError("ZIP entry size mismatch")
            output[name] = data
        if offset != eocd[6] or list(output)[:3] != [
            "manifest.json",
            "manifest.sha256",
            "sources.json",
        ]:
            raise PlaylistBundleError("Unexpected ZIP layout")
    return output


def inspect_playlist_bundle(document: bytes) -> PlaylistBundle:
    try:
        return _inspect(document)
    except PlaylistBundleError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        RecursionError,
        UnicodeError,
        OSError,
        BadZipFile,
        struct.error,
    ):
        raise PlaylistBundleError(
            "Invalid portable playlist ZIP, JSON, capability metadata or JPEG"
        ) from None


def _inspect(document: bytes) -> PlaylistBundle:
    files = _files(document)
    raw = files["manifest.json"]
    if files["manifest.sha256"] != (hashlib.sha256(raw).hexdigest() + "\n").encode("ascii"):
        raise PlaylistBundleError("Manifest SHA256 mismatch")
    root = exact_object(strict_json(raw), {"format", "version", "playlist", "files"})
    if (
        root["format"] != "news-bulletin-playlist.playlist"
        or type(root["version"]) is not int
        or root["version"] != 1
    ):
        raise PlaylistBundleError("Unsupported playlist format or version")
    p = exact_object(
        root["playlist"],
        {
            "portable_id",
            "original_id",
            "kind",
            "display_name",
            "description",
            "configuration",
            "countries",
            "languages",
            "source_ids",
            "ordering",
            "retention_hours",
            "max_episodes",
            "duration_policy",
            "cover",
        },
    )
    if portable_id(p["portable_id"]) is None:
        raise PlaylistBundleError("Portable UUIDv4 is required")
    _id(p["original_id"])
    for field, limit in (("display_name", 4096), ("description", 65536)):
        if not isinstance(p[field], str) or len(p[field]) > limit or "\x00" in p[field]:
            raise PlaylistBundleError("Invalid portable playlist text")
        p[field].encode("utf-8")
    # Draft names must remain editable and canonical; reject rather than trim.
    if not p["display_name"].strip() or p["display_name"] != p["display_name"].strip():
        raise PlaylistBundleError("display_name cannot be represented without trimming")
    _tags(p["countries"], r"[A-Z]{2}")
    _tags(p["languages"], r"[a-z]{2,3}(?:-(?:[A-Z]{2}|[0-9]{3}))?")
    ids = p["source_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or len(set(map(_id, ids))) != len(ids):
        raise PlaylistBundleError("Invalid durable source assignments")
    _integer(p["retention_hours"])
    _integer(p["max_episodes"])
    if p["ordering"] != "edition_at_desc":
        raise PlaylistBundleError("Unsupported ordering")
    duration = exact_object(p["duration_policy"], {"min_seconds", "max_seconds", "exceptions"})
    _integer(duration["min_seconds"], 0)
    _integer(duration["max_seconds"])
    if not isinstance(duration["exceptions"], list) or len(duration["exceptions"]) > 100:
        raise PlaylistBundleError("Invalid duration exceptions")
    exceptions = tuple(parse_exception(e) for e in duration["exceptions"])
    DurationPolicy(duration["max_seconds"], exceptions, duration["min_seconds"])
    if any(e.source_id not in ids for e in exceptions):
        raise PlaylistBundleError("Duration exception references an unassigned source")
    records = inspect_sources(files["sources.json"])
    if {r.source.id if isinstance(r, PortableSource) else r.id for r in records} != set(ids):
        raise PlaylistBundleError("Source closure does not exactly match assignments")
    cover = exact_object(
        p["cover"], {"original_id", "origin", "path", "media_type", "size", "sha256", "attribution"}
    )
    _id(cover["original_id"])
    if (
        not isinstance(cover["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", cover["sha256"]) is None
        or cover["media_type"] != "image/jpeg"
    ):
        raise PlaylistBundleError("Invalid cover identity")
    path = f"covers/{cover['sha256']}.jpg"
    if cover["path"] != path or set(files) != {
        "manifest.json",
        "manifest.sha256",
        "sources.json",
        path,
    }:
        raise PlaylistBundleError("Inconsistent cover reference")
    descriptors = exact_object(root["files"], {"sources.json", path})
    for name in ("sources.json", path):
        descriptor = exact_object(descriptors[name], {"size", "sha256"})
        _integer(descriptor["size"])
        if descriptor != _digest(files[name]):
            raise PlaylistBundleError("Embedded file size or SHA256 mismatch")
    _integer(cover["size"])
    if {k: cover[k] for k in ("size", "sha256")} != _digest(files[path]):
        raise PlaylistBundleError("Cover size or SHA256 mismatch")
    validate_canonical_jpeg(files[path])
    validate_attribution(cover["attribution"])
    metadata = canonical_json(
        {
            "original_id": p["original_id"],
            "kind": p["kind"],
            "configuration": p["configuration"],
            "cover_id": cover["original_id"],
            "cover_origin": cover["origin"],
            "cover_sha256": cover["sha256"],
            "attribution": cover["attribution"],
        }
    ).decode("utf-8")
    from news_bulletin_playlist.portable_playlist_metadata import validate_metadata

    validate_metadata(metadata)
    return PlaylistBundle(p, records, files["sources.json"], files[path], exceptions, metadata)
