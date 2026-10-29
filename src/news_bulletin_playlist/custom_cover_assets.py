"""Bounded, metadata-free persistent owner artwork for custom playlists.

Only opaque content-addressed cover IDs are exposed to callers. No path or
original filename from a browser request is used for storage or retrieval.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import stat
from contextlib import suppress
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

UPLOAD_COVER_PREFIX = "uploaded_"
UPLOADED_COVER_ID_PATTERN = re.compile(r"uploaded_[0-9a-f]{64}\Z")
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_PIXELS = 16_000_000
MAX_DIMENSION = 4096
MIN_DIMENSION = 256
OUTPUT_SIZE = 512
# Spotify accepts a 256 KiB *base64* payload; the on-disk JPEG must be
# smaller than the corresponding decoded byte budget.
MAX_JPEG_BYTES = 192 * 1024


class CustomCoverAssetError(ValueError):
    """Safe, public-facing owner-image validation/storage error."""


def is_uploaded_cover_id(value: str) -> bool:
    return isinstance(value, str) and UPLOADED_COVER_ID_PATTERN.fullmatch(value) is not None


def _canonical_jpeg(raw: bytes) -> bytes:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_SOURCE_BYTES:
        raise CustomCoverAssetError("Image upload must be between 1 byte and 2 MiB")
    try:
        with Image.open(io.BytesIO(raw)) as original:
            if original.format not in {"JPEG", "PNG"}:
                raise CustomCoverAssetError("Only JPEG and PNG images are supported")
            width, height = original.size
            if (
                min(width, height) < MIN_DIMENSION
                or max(width, height) > MAX_DIMENSION
                or width * height > MAX_SOURCE_PIXELS
            ):
                raise CustomCoverAssetError("Image dimensions are outside the permitted range")
            # Decompress under the pixel cap, orient by EXIF and throw the metadata
            # away. The resulting file has no GPS/EXIF/ICC/comment payload.
            original.load()
            oriented = ImageOps.exif_transpose(original)
            if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                background = Image.new("RGB", oriented.size, (15, 27, 48))
                image = oriented.convert("RGBA")
                background.paste(image, mask=image.getchannel("A"))
            else:
                background = oriented.convert("RGB")
            square = ImageOps.fit(background, (OUTPUT_SIZE, OUTPUT_SIZE), Image.Resampling.LANCZOS)
            for quality in (88, 82, 76, 70, 60, 50):
                output = io.BytesIO()
                square.save(output, format="JPEG", quality=quality, optimize=True)
                encoded = output.getvalue()
                if len(encoded) <= MAX_JPEG_BYTES:
                    return encoded
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise CustomCoverAssetError("Could not decode a safe JPEG or PNG image") from exc
    raise CustomCoverAssetError("Image cannot fit the Spotify cover-size limit")


def validate_canonical_jpeg(data: bytes) -> None:
    """Verify portable JPEG bytes under decode caps; never transform metadata or pixels."""
    if (
        not isinstance(data, bytes)
        or not 0 < len(data) <= MAX_JPEG_BYTES
        or not data.startswith(b"\xff\xd8")
        or not data.endswith(b"\xff\xd9")
    ):
        raise CustomCoverAssetError("Invalid canonical JPEG bytes")
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if (
                image.format != "JPEG"
                or min(width, height) <= 0
                or max(width, height) > MAX_DIMENSION
                or width * height > MAX_SOURCE_PIXELS
            ):
                raise CustomCoverAssetError("Unsafe canonical JPEG dimensions")
            image.load()
    except OSError, ValueError, Image.DecompressionBombError:
        raise CustomCoverAssetError("Canonical JPEG cannot be decoded safely") from None


class CustomCoverAssetStore:
    """Content-addressed owner-only JPEG storage under the /data persistent mount."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _open_dir(self, *, create: bool) -> int:
        try:
            if create:
                self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            mode = os.fstat(fd).st_mode
            if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o077:
                os.close(fd)
                raise CustomCoverAssetError("Custom cover directory permissions are unsafe")
            return fd
        except OSError as exc:
            raise CustomCoverAssetError("Custom cover storage is unavailable") from exc

    def load(self, cover_id: str) -> bytes:
        if not is_uploaded_cover_id(cover_id):
            raise CustomCoverAssetError("Unknown custom cover identifier")
        directory = self._open_dir(create=False)
        try:
            try:
                fd = os.open(
                    f"{cover_id}.jpg", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
            except OSError as exc:
                raise CustomCoverAssetError("Selected custom cover is missing") from exc
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise CustomCoverAssetError("Custom cover is not a regular file")
                with os.fdopen(fd, "rb", closefd=False) as source:
                    data = source.read(MAX_JPEG_BYTES + 1)
            finally:
                os.close(fd)
            digest = hashlib.sha256(data).hexdigest()
            if (
                not 0 < len(data) <= MAX_JPEG_BYTES
                or f"{UPLOAD_COVER_PREFIX}{digest}" != cover_id
                or not data.startswith(b"\xff\xd8")
                or not data.endswith(b"\xff\xd9")
            ):
                raise CustomCoverAssetError("Stored custom cover failed integrity verification")
            return data
        finally:
            os.close(directory)

    def install_canonical(self, jpeg: bytes) -> str:
        """Durably publish verified exact bytes without overwriting an existing asset.

        Safe orphan files may remain after a failed state commit. A retry verifies and
        syncs them before referencing them; no new recovery journal or asset GC is used.
        """
        from news_bulletin_playlist.file_durability import fsync_directory

        validate_canonical_jpeg(jpeg)
        cover_id = f"{UPLOAD_COVER_PREFIX}{hashlib.sha256(jpeg).hexdigest()}"
        directory = self._open_dir(create=True)
        filename = f"{cover_id}.jpg"
        temp = f".{secrets.token_hex(16)}.tmp"
        try:
            try:
                fd = os.open(
                    temp,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                with os.fdopen(fd, "wb") as output:
                    output.write(jpeg)
                    output.flush()
                    os.fsync(output.fileno())
                with suppress(FileExistsError):
                    os.link(
                        temp,
                        filename,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                if self.load(cover_id) != jpeg:
                    raise CustomCoverAssetError("Existing canonical cover content differs")
                fd = os.open(
                    filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.unlink(temp, dir_fd=directory)
                os.fsync(directory)
                fsync_directory(self.root.parent)
                return cover_id
            except OSError:
                raise CustomCoverAssetError(
                    "Canonical cover persistence failed; configuration was not committed"
                ) from None
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=directory)
            os.close(directory)

    def save(self, source_bytes: bytes) -> str:
        jpeg = _canonical_jpeg(source_bytes)
        cover_id = f"{UPLOAD_COVER_PREFIX}{hashlib.sha256(jpeg).hexdigest()}"
        directory = self._open_dir(create=True)
        filename = f"{cover_id}.jpg"
        temp_name = f".{secrets.token_hex(16)}.tmp"
        try:
            try:
                existing = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                existing = None
            except OSError as exc:
                raise CustomCoverAssetError("Cannot inspect existing cover safely") from exc
            if existing is not None:
                try:
                    with os.fdopen(existing, "rb", closefd=False) as old:
                        if old.read(MAX_JPEG_BYTES + 1) != jpeg:
                            raise CustomCoverAssetError("Existing cover ID has invalid content")
                finally:
                    os.close(existing)
                return cover_id

            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
            target = os.open(temp_name, flags, 0o600, dir_fd=directory)
            try:
                with os.fdopen(target, "wb", closefd=False) as output:
                    output.write(jpeg)
                    output.flush()
                    os.fsync(target)
            finally:
                os.close(target)
            os.replace(temp_name, filename, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            return cover_id
        except OSError as exc:
            raise CustomCoverAssetError("Could not persist the custom cover") from exc
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temp_name, dir_fd=directory)
            os.close(directory)
