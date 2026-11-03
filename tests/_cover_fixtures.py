"""Deterministic offline JPEG fixture used by playlist cover tests."""

from __future__ import annotations

from io import BytesIO

from PIL import Image


def _artwork() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (512, 512), "navy").save(buffer, format="JPEG")
    return buffer.getvalue()
