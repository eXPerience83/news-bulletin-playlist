"""Deterministic, bounded collection of unchanged portable playlist ZIP v3 files."""

from __future__ import annotations

import hashlib
import io
import re
import stat
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from news_bulletin_playlist.playlist_bundle import inspect_playlist_bundle_v3
from news_bulletin_playlist.portable_playlists import (
    MAX_PLAYLIST_ZIP_V2_BYTES,
    canonical_json,
)

MAX_MEMBERS = 256
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 256 * 1024
MAX_ARCHIVE_BYTES = MAX_PAYLOAD_BYTES + 2 * MAX_INDEX_BYTES
_INDEX = "manifest.json"
_HASH = "manifest.sha256"


class CollectionError(ValueError):
    """Safe collection validation error."""


@dataclass(frozen=True, slots=True)
class Member:
    portable_id: str
    kind: str
    document: bytes

    @property
    def path(self) -> str:
        return f"playlists/{self.portable_id}.zip"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(archive: ZipFile, path: str, data: bytes) -> None:
    info = ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    archive.writestr(info, data)


def _members(documents: Iterable[bytes]) -> list[Member]:
    members: list[Member] = []
    seen: set[str] = set()
    total = 0
    for document in documents:
        total += len(document)
        if len(members) >= MAX_MEMBERS or total > MAX_PAYLOAD_BYTES:
            raise CollectionError("Collection exceeds aggregate limits")
        if not 0 < len(document) <= MAX_PLAYLIST_ZIP_V2_BYTES:
            raise CollectionError("Collection member exceeds size limit")
        try:
            playlist = inspect_playlist_bundle_v3(document).playlist
        except ValueError as exc:
            raise CollectionError("Invalid individual ZIP v3 member") from exc
        identity = str(playlist["portable_id"])
        kind = str(playlist["kind"])
        if identity in seen:
            raise CollectionError("Duplicate collection UUID")
        seen.add(identity)
        members.append(Member(identity, kind, document))
    return sorted(members, key=lambda member: member.portable_id)


def _encode(members: list[Member]) -> bytes:
    manifest = canonical_json(
        {
            "format": "news-bulletin-playlist.playlist-collection",
            "version": 1,
            "count": len(members),
            "members": [
                {
                    "portable_id": member.portable_id,
                    "kind": member.kind,
                    "path": member.path,
                    "size": len(member.document),
                    "sha256": _digest(member.document),
                }
                for member in members
            ],
        }
    )
    if len(manifest) > MAX_INDEX_BYTES:
        raise CollectionError("Collection manifest exceeds size limit")
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED, allowZip64=False) as archive:
        _write(archive, _INDEX, manifest)
        _write(archive, _HASH, (_digest(manifest) + "\n").encode("ascii"))
        for member in members:
            _write(archive, member.path, member.document)
    result = output.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise CollectionError("Collection archive exceeds size limit")
    return result


def build_collection(documents: Iterable[bytes]) -> bytes:
    """Validate all individual packages before emitting a collection."""
    return _encode(_members(documents))


def inspect_collection(document: bytes) -> tuple[Member, ...]:
    """Validate bounded members and the exact canonical outer envelope.

    Regenerating the outer archive checks the manifest, digests, paths, local
    headers and central-directory metadata without trusting manifest fields.
    No files are extracted or installed.
    """
    if not 22 < len(document) <= MAX_ARCHIVE_BYTES:
        raise CollectionError("Collection archive exceeds size limit")
    try:
        eocd = struct.unpack("<4s4H2IH", document[-22:])
        if (
            eocd[:3] != (b"PK\x05\x06", 0, 0)
            or eocd[3] != eocd[4]
            or not 2 <= eocd[3] <= MAX_MEMBERS + 2
            or eocd[7] != 0
            or not 46 * eocd[3] <= eocd[5] <= 96 * eocd[3]
            or eocd[5] + eocd[6] != len(document) - 22
        ):
            raise CollectionError("Invalid collection ZIP envelope")
        with ZipFile(io.BytesIO(document), allowZip64=False) as archive:
            infos = archive.infolist()
            if len(infos) != eocd[3]:
                raise CollectionError("Invalid collection entry count")
            names: set[str] = set()
            total = 0
            payloads: list[bytes] = []
            for info in infos:
                name = info.filename
                if name == _INDEX:
                    limit = MAX_INDEX_BYTES
                elif name == _HASH:
                    limit = 65
                elif re.fullmatch(r"playlists/[0-9a-f-]{36}\.zip", name):
                    limit = MAX_PLAYLIST_ZIP_V2_BYTES
                    total += info.file_size
                else:
                    raise CollectionError("Invalid collection path")
                if (
                    name in names
                    or info.compress_type != ZIP_STORED
                    or info.flag_bits
                    or info.compress_size != info.file_size
                    or not 0 < info.file_size <= limit
                    or total > MAX_PAYLOAD_BYTES
                ):
                    raise CollectionError("Invalid collection ZIP entry")
                names.add(name)
                if name not in {_INDEX, _HASH}:
                    payloads.append(archive.read(info))
        members = _members(payloads)
        if _encode(members) != document:
            raise CollectionError("Collection manifest or ZIP envelope is not canonical")
        return tuple(members)
    except CollectionError:
        raise
    except (BadZipFile, OSError, RuntimeError, ValueError, struct.error) as exc:
        raise CollectionError("Invalid collection archive") from exc
