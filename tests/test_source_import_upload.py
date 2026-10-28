"""Bounded multipart envelope around the existing strict source JSON codec."""

import pytest
from test_source_import_http import _multipart

from news_bulletin_playlist.source_import_web import parse_source_import_upload


def test_raw_document_and_untrusted_filename_are_not_changed():
    content_type, body = _multipart("x" * 32, b'{"original": "bytes"}\n')
    assert parse_source_import_upload(content_type, body) == ("x" * 32, b'{"original": "bytes"}\n')


@pytest.mark.parametrize(
    "change",
    [
        "missing_end",
        "wrong_boundary",
        "duplicate",
        "extra",
        "encoded",
        "nested",
        "mime",
        "non_ascii",
        "huge_headers",
        "duplicate_header",
    ],
)
def test_malformed_upload_fails_closed(change):
    content_type, body = _multipart("x" * 32, b"{}")
    if change == "missing_end":
        body = body[:-5]
    elif change == "wrong_boundary":
        content_type += "not-the-boundary"
    elif change == "duplicate":
        body = body.replace(b'name="document"', b'name="csrf_token"')
    elif change == "extra":
        body = body.replace(b'name="document"', b'name="other"')
    elif change == "encoded":
        body = body.replace(
            b"Content-Type: application/json",
            b"Content-Transfer-Encoding: base64\r\nContent-Type: application/json",
        )
    elif change == "nested":
        body = body.replace(b"application/json", b"multipart/mixed; boundary=nested")
    elif change == "mime":
        body = body.replace(b"application/json", b"text/html")
    elif change == "non_ascii":
        body = body.replace(b"x" * 32, b"\xff" * 32)
    elif change == "huge_headers":
        content_type = "a" * 181
    else:
        body = body.replace(
            b"Content-Type: application/json",
            b"Content-Type: application/json\r\nContent-Type: text/plain",
        )
    with pytest.raises(ValueError):
        parse_source_import_upload(content_type, body)
