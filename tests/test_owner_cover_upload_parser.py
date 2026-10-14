"""Strict binary multipart input for custom owner cover uploads."""

from __future__ import annotations

import pytest

from news_bulletin_playlist.owner_cover_upload import (
    CoverUploadRequestError,
    parse_owner_cover_multipart,
)


def _multipart(
    *,
    csrf: str = "good-token",
    target_kind: str = "draft",
    target_id: str = "custom_example",
    image: bytes = b"image-bytes",
    image_type: str = "image/png",
    repeated_image: bool = False,
    unknown_field: bool = False,
    end: bool = True,
) -> tuple[str, bytes]:
    boundary = b"abcdefghijkl"
    fields: list[bytes] = []
    for key, value in (
        ("csrf_token", csrf.encode()),
        ("target_kind", target_kind.encode()),
        ("target_id", target_id.encode()),
    ):
        fields.append(
            b"--"
            + boundary
            + b"\r\n"
            + f'Content-Disposition: form-data; name="{key}"'.encode()
            + b"\r\n\r\n"
            + value
            + b"\r\n"
        )
    image_field = (
        b"--"
        + boundary
        + b"\r\n"
        + b'Content-Disposition: form-data; name="image"; filename="image.png"\r\n'
        + f"Content-Type: {image_type}".encode()
        + b"\r\n\r\n"
        + image
        + b"\r\n"
    )
    fields.append(image_field)
    if repeated_image:
        fields.append(image_field)
    if unknown_field:
        fields.append(
            b"--"
            + boundary
            + b'\r\nContent-Disposition: form-data; name="secret"'
            + b"\r\n\r\nnope\r\n"
        )
    if end:
        fields.append(b"--" + boundary + b"--\r\n")
    return "multipart/form-data; boundary=abcdefghijkl", b"".join(fields)


def test_bounded_owner_cover_multipart_roundtrips_binary_without_filename_usage() -> None:
    kind, data = _multipart(image=b"\xff\xd8\x00\x99\xff\xd9", image_type="image/jpeg")
    assert parse_owner_cover_multipart(kind, data) == (
        "good-token",
        "draft",
        "custom_example",
        b"\xff\xd8\x00\x99\xff\xd9",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"repeated_image": True},
        {"unknown_field": True},
        {"image_type": "image/svg+xml"},
        {"end": False},
        {"csrf": ""},
        {"target_kind": "builtin"},
        {"image": b""},
    ],
)
def test_invalid_fields_are_rejected(changes: dict[str, object]) -> None:
    kind, data = _multipart(**changes)
    with pytest.raises(CoverUploadRequestError):
        parse_owner_cover_multipart(kind, data)


def test_invalid_boundary_and_body_limits_fail_closed() -> None:
    kind, data = _multipart()
    for invalid_type in (
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=../unsafe",
        "multipart/form-data; boundary=abc; foo=bar",
    ):
        with pytest.raises(CoverUploadRequestError):
            parse_owner_cover_multipart(invalid_type, data)
    with pytest.raises(CoverUploadRequestError):
        parse_owner_cover_multipart(kind, b"?" * (2 * 1024 * 1024 + 8193))
