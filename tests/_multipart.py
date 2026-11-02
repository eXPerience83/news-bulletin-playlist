"""Byte-preserving multipart envelope for test-only import workflows."""

from __future__ import annotations


def import_upload(
    token: str, document: bytes, *, media_type: str = "application/json"
) -> tuple[str, bytes]:
    boundary = "source-import-test-boundary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n{token}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
            'filename="../../managed-state.json"\r\n'
            f"Content-Type: {media_type}\r\n\r\n"
        ).encode()
        + document
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return "multipart/form-data; boundary=" + boundary, body
