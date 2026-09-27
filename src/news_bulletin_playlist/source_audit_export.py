"""Bounded, sanitized ZIP export for configured Source Audit reports."""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import UTC, datetime

from news_bulletin_playlist import __version__
from news_bulletin_playlist.source_admin import (
    MAX_SOURCE_AUDIT_BATCH_SOURCES,
    SourceAuditBatch,
    SourceAuditBatchItem,
)
from news_bulletin_playlist.source_inspection import serialize_source_inspection

SOURCE_AUDIT_EXPORT_SCHEMA_VERSION = 1
_SAFE_ARCHIVE_SOURCE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def build_source_audit_bundle(batch: SourceAuditBatch, *, build_revision: str) -> bytes:
    """Serialize one bounded batch result without raw provider responses or credentials."""
    reports: list[tuple[str, bytes]] = []
    source_documents = []
    succeeded = failed = not_attempted = 0

    for item in batch.items:
        source_id = str(item.source_id)
        if _SAFE_ARCHIVE_SOURCE_ID.fullmatch(source_id) is None:
            raise ValueError("source audit export contains an unsafe source id")
        report_path: str | None = None
        public_status: str | None = None
        if item.status == "succeeded":
            if item.report is None or item.report.source_id != item.source_id:
                raise ValueError("successful source audit item is missing its matching report")
            report_path = f"sources/{source_id}.json"
            reports.append((report_path, serialize_source_inspection(item.report)))
            public_status = item.report.public_status
            succeeded += 1
        elif item.status == "failed":
            if item.report is not None or item.failure is None:
                raise ValueError("failed source audit item has an invalid result shape")
            failed += 1
        elif item.status == "not_attempted":
            if item.report is not None or item.not_attempted_reason is None:
                raise ValueError("unattempted source audit item has an invalid result shape")
            not_attempted += 1
        else:
            raise ValueError("source audit item has an invalid status")

        source_documents.append(
            _source_document(
                item,
                report_path=report_path,
                public_status=public_status,
            )
        )

    total = len(batch.items)
    if total > MAX_SOURCE_AUDIT_BATCH_SOURCES:
        raise ValueError("source audit export exceeds its configured source bound")
    manifest = {
        "schema_version": SOURCE_AUDIT_EXPORT_SCHEMA_VERSION,
        "format": "news-bulletin-playlist-source-audits-v1",
        "generated_at": _timestamp(batch.generated_at),
        "application_version": __version__,
        "build_revision": build_revision,
        "max_sources": MAX_SOURCE_AUDIT_BATCH_SOURCES,
        "counts": {
            "total": total,
            "attempted": succeeded + failed,
            "succeeded": succeeded,
            "failed": failed,
            "not_attempted": not_attempted,
        },
        "public_observation_suppressed_after_source": (
            None
            if batch.public_observation_suppressed_after is None
            else str(batch.public_observation_suppressed_after)
        ),
        "sources": source_documents,
        "files": ["manifest.json", *[name for name, _payload in reports]],
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _writestr(archive, "manifest.json", _json_bytes(manifest))
        for name, payload in reports:
            _writestr(archive, name, payload)
    return buffer.getvalue()


def source_audit_archive_filename(generated_at: datetime) -> str:
    observed = generated_at.astimezone(UTC)
    return f"source-audits-{observed.strftime('%Y%m%dT%H%M%SZ')}.zip"


def _source_document(
    item: SourceAuditBatchItem,
    *,
    report_path: str | None,
    public_status: str | None,
) -> dict[str, object]:
    return {
        "source_id": str(item.source_id),
        "display_name": item.display_name,
        "ownership": item.ownership,
        "enabled": item.enabled,
        "status": item.status,
        "report_path": report_path,
        "public_status": public_status,
        "failure": None if item.failure is None else item.failure.value,
        "response_reason": (
            None if item.response_reason is None else item.response_reason.value
        ),
        "not_attempted_reason": item.not_attempted_reason,
    }


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _writestr(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, content)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
