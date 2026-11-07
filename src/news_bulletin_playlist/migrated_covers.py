"""Durable, byte-preserving inherited JPEG inventory for forward-only v11.

A content-addressed /data/custom-covers asset and a small authenticated-local
manifest preserve each inherited bundled JPEG and its original attribution.
The manifest is NOT a provider destination, executable preset or backup.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from news_bulletin_playlist.custom_cover_assets import (
    CustomCoverAssetError,
    CustomCoverAssetStore,
    is_uploaded_cover_id,
)
from news_bulletin_playlist.file_durability import fsync_directory
from news_bulletin_playlist.portable_playlist_metadata import validate_attribution

MIGRATED_COVERS_DIR = "migrated-covers"
_INDEX_NAME = "index.json"
_FORMAT = "news-bulletin-playlist.migrated-covers"
_MAX_INDEX_BYTES = 64 * 1024
_COVER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class MigratedCoverError(ValueError):
    """Corrupt or unavailable owner artwork must never fall back silently."""


class _CoverLoader(Protocol):
    def load(self, cover_id: str) -> tuple[bytes, dict[str, object]]: ...


class MigratedCoverAssets:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / MIGRATED_COVERS_DIR
        self.assets = CustomCoverAssetStore(data_dir / "custom-covers")

    def _directory(self, *, create: bool) -> int | None:
        if create:
            try:
                self.root.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise MigratedCoverError("Migrated cover directory is unavailable") from exc
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MigratedCoverError("Migrated cover directory is unsafe") from exc
        mode = os.fstat(fd).st_mode
        if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o077:
            os.close(fd)
            raise MigratedCoverError("Migrated cover directory permissions are unsafe")
        return fd

    @staticmethod
    def _canonical(value: object) -> bytes:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        )

    @staticmethod
    def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, value in pairs:
            if name in result:
                raise MigratedCoverError("Duplicate migrated cover manifest key")
            result[name] = value
        return result

    def _read_index(self) -> dict[str, tuple[str, dict[str, object]]] | None:
        directory = self._directory(create=False)
        if directory is None:
            return None
        try:
            try:
                fd = os.open(_INDEX_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                return None
            try:
                mode = os.fstat(fd).st_mode
                if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
                    raise MigratedCoverError("Migrated cover manifest is not a private regular file")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(_MAX_INDEX_BYTES + 1)
            finally:
                os.close(fd)
        except OSError as exc:
            raise MigratedCoverError("Migrated cover manifest cannot be read") from exc
        finally:
            os.close(directory)
        if not 0 < len(raw) <= _MAX_INDEX_BYTES:
            raise MigratedCoverError("Invalid migrated cover manifest size")
        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=self._unique)
            if not isinstance(payload, dict) or set(payload) != {"format", "version", "covers"}:
                raise MigratedCoverError("Invalid migrated cover manifest fields")
            if payload["format"] != _FORMAT or type(payload["version"]) is not int:
                raise MigratedCoverError("Unsupported migrated cover manifest")
            if payload["version"] != 1 or not isinstance(payload["covers"], list):
                raise MigratedCoverError("Unsupported migrated cover manifest version")
            if len(payload["covers"]) > 100:
                raise MigratedCoverError("Too many migrated covers")
            entries: dict[str, tuple[str, dict[str, object]]] = {}
            for entry in payload["covers"]:
                if not isinstance(entry, dict) or set(entry) != {
                    "cover_id",
                    "asset_id",
                    "attribution",
                }:
                    raise MigratedCoverError("Invalid migrated cover record")
                cover_id, asset_id = entry["cover_id"], entry["asset_id"]
                if (
                    not isinstance(cover_id, str)
                    or _COVER_ID.fullmatch(cover_id) is None
                    or is_uploaded_cover_id(cover_id)
                    or cover_id in entries
                    or not isinstance(asset_id, str)
                    or not is_uploaded_cover_id(asset_id)
                ):
                    raise MigratedCoverError("Invalid migrated cover ID or duplicate")
                attribution = validate_attribution(entry["attribution"])
                entries[cover_id] = (asset_id, attribution)
            if raw != self._canonical(payload):
                raise MigratedCoverError("Noncanonical migrated cover manifest")
            return entries
        except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise MigratedCoverError("Cannot validate migrated cover manifest") from exc

    def load(self, cover_id: str) -> tuple[bytes, dict[str, object]] | None:
        records = self._read_index()
        if records is None or cover_id not in records:
            return None
        asset_id, attribution = records[cover_id]
        try:
            data = self.assets.load(asset_id)
        except (CustomCoverAssetError, OSError) as exc:
            raise MigratedCoverError("Persisted inherited cover is unavailable or corrupt") from exc
        return data, attribution

    def materialize(self, cover_ids: set[str], loader: _CoverLoader) -> None:
        """Publish all inherited cover bytes BEFORE creating irreversible v11 authority.

        An interrupted attempt may leave safe content-addressed orphan files.
        A retry compares the existing immutable manifest; it never overwrites.
        """
        requested = {item for item in cover_ids if not is_uploaded_cover_id(item)}
        if len(requested) > 100:
            raise MigratedCoverError("Too many inherited covers")
        prior = self._read_index()
        if prior is not None:
            if set(prior) != requested:
                raise MigratedCoverError("Existing inherited cover inventory differs")
            for cover_id in prior:
                if self.load(cover_id) is None:
                    raise MigratedCoverError("Inherited cover unexpectedly missing")
            for cover_id in cover_ids - requested:
                self.assets.load(cover_id)
            return

        entries: list[dict[str, object]] = []
        for cover_id in sorted(cover_ids):
            if is_uploaded_cover_id(cover_id):
                self.assets.load(cover_id)
                continue
            if _COVER_ID.fullmatch(cover_id) is None:
                raise MigratedCoverError("Unsafe inherited cover identifier")
            try:
                jpeg, attribution = loader.load(cover_id)
                installed_id = self.assets.install_canonical(jpeg)
                validated = validate_attribution(attribution)
            except (OSError, ValueError, CustomCoverAssetError) as exc:
                raise MigratedCoverError("Cannot preserve inherited cover bytes") from exc
            entries.append(
                {"cover_id": cover_id, "asset_id": installed_id, "attribution": validated}
            )
        payload = self._canonical({"format": _FORMAT, "version": 1, "covers": entries})
        if len(payload) > _MAX_INDEX_BYTES:
            raise MigratedCoverError("Migrated cover manifest exceeds size limit")
        directory = self._directory(create=True)
        assert directory is not None
        temporary = ".migrated-covers-" + secrets.token_hex(12) + ".tmp"
        try:
            try:
                fd = os.open(
                    temporary,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(
                        temporary,
                        _INDEX_NAME,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    # A distinct index cannot be overwritten, even by a retry.
                    existing = self._read_index()
                    if existing != {
                        e["cover_id"]: (e["asset_id"], e["attribution"]) for e in entries
                    }:
                        raise MigratedCoverError("Conflicting inherited cover manifest")
                os.fsync(directory)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
        except OSError as exc:
            raise MigratedCoverError("Inherited cover manifest was not committed") from exc
        finally:
            os.close(directory)
        fsync_directory(self.root.parent)
