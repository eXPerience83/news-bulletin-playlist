"""Dedicated bounded multipart parser for owner-only cover uploads.

Never reuse the ordinary 4 KiB admin text-form reader for binary payloads.
"""

from __future__ import annotations

import re
from email import policy
from email.parser import BytesParser

from news_bulletin_playlist.custom_cover_assets import MAX_SOURCE_BYTES

MAX_MULTIPART_BYTES = MAX_SOURCE_BYTES + 8192
_BOUNDARY = re.compile(r"[A-Za-z0-9_.'()+,/:=?-]{1,100}\Z")
_TEXT_LIMIT = 128
_FIELD_NAMES = {"csrf_token", "target_kind", "target_id", "image"}


class CoverUploadRequestError(ValueError):
    """Bounded and sanitized request validation error."""


def parse_owner_cover_multipart(content_type: str, body: bytes) -> tuple[str, str, str, bytes]:
    """Extract CSRF, target-kind, target-ID and image from a strict multipart body."""
    if not isinstance(content_type, str) or len(content_type) > 180:
        raise CoverUploadRequestError("Invalid image upload content type")
    match = re.fullmatch(
        r"multipart/form-data;\s*boundary=(?:\"([^\"\\]{1,100})\"|([^;\s]{1,100}))",
        content_type,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise CoverUploadRequestError("Image upload requires multipart/form-data")
    boundary = match[1] or match[2]
    if _BOUNDARY.fullmatch(boundary) is None:
        raise CoverUploadRequestError("Invalid multipart boundary")
    if not 0 < len(body) <= MAX_MULTIPART_BYTES:
        raise CoverUploadRequestError("Image upload exceeds 2 MiB request limit")
    # Only accept a complete framed message. Email MIME tolerates some truncation,
    # which is unsuitable for CSRF-protected uploads.
    boundary_bytes = boundary.encode("ascii")
    if not body.startswith(b"--" + boundary_bytes + b"\r\n"):
        raise CoverUploadRequestError("Incomplete multipart upload")
    if not body.rstrip(b"\r\n").endswith(b"--" + boundary_bytes + b"--"):
        raise CoverUploadRequestError("Incomplete multipart upload")
    headers = (
        b"MIME-Version: 1.0\r\nContent-Type: multipart/form-data; boundary="
        + boundary_bytes
        + b"\r\n\r\n"
    )
    message = BytesParser(policy=policy.default).parsebytes(headers + body)
    if not message.is_multipart() or message.defects:
        raise CoverUploadRequestError("Invalid multipart upload")
    parts = message.iter_parts()
    fields: dict[str, bytes] = {}
    for part in parts:
        if part.defects or part.is_multipart():
            raise CoverUploadRequestError("Invalid image upload field")
        if part.get_content_disposition() != "form-data":
            raise CoverUploadRequestError("Invalid image upload field")
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or name not in _FIELD_NAMES or name in fields:
            raise CoverUploadRequestError("Invalid or duplicate image upload field")
        cte = part.get("Content-Transfer-Encoding", "binary").lower()
        if cte not in {"binary", "8bit"}:
            raise CoverUploadRequestError("Unsupported upload transfer encoding")
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise CoverUploadRequestError("Invalid image upload field")
        if name == "image":
            if part.get_content_type() not in {"image/jpeg", "image/png"}:
                raise CoverUploadRequestError("Only JPEG/PNG image upload is allowed")
            if not 0 < len(payload) <= MAX_SOURCE_BYTES:
                raise CoverUploadRequestError("Image upload exceeds 2 MiB limit")
        elif len(payload) > _TEXT_LIMIT or part.get_filename() is not None:
            raise CoverUploadRequestError("Invalid image upload field")
        fields[name] = payload
    if fields.keys() != _FIELD_NAMES:
        raise CoverUploadRequestError("Image upload requires exactly four fields")
    try:
        csrf, target_kind, target_id = (
            fields[key].decode("utf-8", errors="strict")
            for key in ("csrf_token", "target_kind", "target_id")
        )
    except UnicodeDecodeError as exc:
        raise CoverUploadRequestError("Invalid image upload form encoding") from exc
    if target_kind not in {"draft", "managed"} or not csrf or not target_id:
        raise CoverUploadRequestError("Invalid image upload target")
    return csrf, target_kind, target_id, fields["image"]
