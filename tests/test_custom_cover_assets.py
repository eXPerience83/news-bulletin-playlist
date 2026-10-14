"""Owner cover assets never expose original bytes/metadata or filesystem paths."""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from news_bulletin_playlist.custom_cover_assets import (
    MAX_SOURCE_BYTES,
    CustomCoverAssetError,
    CustomCoverAssetStore,
    is_uploaded_cover_id,
)


def _image_bytes(
    *,
    fmt: str = "PNG",
    size: tuple[int, int] = (800, 620),
    metadata: bool = False,
) -> bytes:
    image = Image.new("RGB", size, "#38446a")
    output = io.BytesIO()
    extras = {}
    if fmt == "PNG" and metadata:
        info = PngImagePlugin.PngInfo()
        info.add_text("GPSInfo", "Private location")
        extras["pnginfo"] = info
    image.save(output, format=fmt, **extras)
    return output.getvalue()


def test_jpeg_and_png_are_normalized_to_metadata_free_spotify_jpegs(tmp_path: Path) -> None:
    store = CustomCoverAssetStore(tmp_path / "custom-covers")
    png = _image_bytes(metadata=True)
    cover_id = store.save(png)
    result = store.load(cover_id)

    assert is_uploaded_cover_id(cover_id)
    assert result.startswith(b"\xff\xd8") and result.endswith(b"\xff\xd9")
    assert len(result) <= 192 * 1024
    assert b"Private location" not in result
    with Image.open(io.BytesIO(result)) as loaded:
        assert loaded.size == (512, 512)
        assert loaded.format == "JPEG"
        assert loaded.getexif() == {}

    assert store.save(png) == cover_id  # deterministic and reusable
    assert store.save(_image_bytes(fmt="JPEG")) != cover_id
    assert stat.S_IMODE(store.root.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE((store.root / f"{cover_id}.jpg").stat().st_mode) & 0o077 == 0
    assert len(list(store.root.iterdir())) == 2


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not an image",
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        b"\xff\xd8missing-rest",
        b"X" * (MAX_SOURCE_BYTES + 1),
        _image_bytes(size=(200, 400)),
        _image_bytes(size=(4200, 1000)),
    ],
)
def test_invalid_and_oversized_image_is_rejected_without_writing(
    tmp_path: Path, raw: bytes
) -> None:
    store = CustomCoverAssetStore(tmp_path / "custom-covers")
    with pytest.raises(CustomCoverAssetError):
        store.save(raw)
    assert not store.root.exists()


def test_asset_ids_reject_path_traversal_and_missing_or_corrupted_images(tmp_path: Path) -> None:
    store = CustomCoverAssetStore(tmp_path / "covers")
    cover_id = store.save(_image_bytes())
    for bad_id in (
        "../../passwd",
        "uploaded_" + "f" * 63,
        "uploaded_" + "A" * 64,
        "custom_default",
    ):
        assert not is_uploaded_cover_id(bad_id)
        with pytest.raises(CustomCoverAssetError):
            store.load(bad_id)
    assert is_uploaded_cover_id(cover_id)
    bad_file = store.root / f"{cover_id}.jpg"
    bad_file.write_bytes(b"faked JPEG contents")
    with pytest.raises(CustomCoverAssetError, match="integrity"):
        store.load(cover_id)
    with pytest.raises(CustomCoverAssetError, match="invalid content"):
        store.save(_image_bytes())


def test_symbolic_link_directory_or_destination_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CustomCoverAssetError):
        CustomCoverAssetStore(linked).save(_image_bytes())
    assert list(outside.iterdir()) == []

    real = CustomCoverAssetStore(tmp_path / "real")
    cover_id = real.save(_image_bytes())
    path = real.root / f"{cover_id}.jpg"
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(CustomCoverAssetError):
        real.load(cover_id)
    with pytest.raises(CustomCoverAssetError):
        real.save(_image_bytes())
    assert not any(p.name.endswith(".tmp") for p in real.root.iterdir())


def test_store_requires_owner_only_permissions(tmp_path: Path) -> None:
    root = tmp_path / "covers"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    store = CustomCoverAssetStore(root)
    with pytest.raises(CustomCoverAssetError, match="permissions"):
        store.save(_image_bytes())
