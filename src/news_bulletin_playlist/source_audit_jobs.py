"""Bounded, single-flight, in-memory jobs for read-only source audit exports.

The HTTP handler only submits work and serves progress/results; provider I/O remains
in one daemon worker. Completed artifacts are retained only until the next job or
expiry, never persisted to disk. No account or provider secrets appear in status.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from collections.abc import Callable
from typing import TYPE_CHECKING

from news_bulletin_playlist.source_admin import (
    MAX_SOURCE_AUDIT_BATCH_SOURCES,
    SourceAuditBatchLimitError,
)
from news_bulletin_playlist.source_audit_export import (
    build_source_audit_bundle,
    source_audit_archive_filename,
)

if TYPE_CHECKING:
    from news_bulletin_playlist.source_admin import SourceAdminService

JOB_MAX_SECONDS = 600
JOB_RESULT_SECONDS = 1800
JOB_MAX_ARCHIVE_BYTES = 16 * 1024 * 1024


class AuditJobBusyError(RuntimeError):
    """Only one audit may run at a time."""


class _AuditJobCancelled(Exception):
    pass


class _AuditJobDeadline(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AuditJobSnapshot:
    job_id: str
    status: str
    completed: int
    total: int
    filename: str | None
    payload: bytes | None
    error: str | None


class SourceAuditJobs:
    """Single-flight background work with small, read-only status snapshots."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: AuditJobSnapshot | None = None
        self._started = 0.0
        self._finished = 0.0
        self._cancel = threading.Event()
        self._closed = False
        self._worker: threading.Thread | None = None

    def start(
        self,
        service: SourceAdminService,
        *,
        build_revision: str,
        on_event: Callable[[str, int, int], None] | None = None,
    ) -> str:
        with self._lock:
            if self._closed or (self._job is not None and self._job.status == "running"):
                raise AuditJobBusyError("A source audit is already running")
            job_id = secrets.token_urlsafe(24)
            self._job = AuditJobSnapshot(job_id, "running", 0, 0, None, None, None)
            self._started = time.monotonic()
            self._finished = 0.0
            self._cancel = threading.Event()
            worker = threading.Thread(
                target=self._run,
                args=(job_id, service, build_revision, self._cancel, on_event),
                name="source-audit-export",
                daemon=True,
            )
            self._worker = worker
            worker.start()
            return job_id

    def get(self, job_id: str) -> AuditJobSnapshot | None:
        with self._lock:
            if self._job is None or not secrets.compare_digest(self._job.job_id, job_id):
                return None
            if (
                self._job.status != "running"
                and time.monotonic() - self._finished > JOB_RESULT_SECONDS
            ):
                self._job = None
                return None
            return self._job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            if self._job is None or not secrets.compare_digest(self._job.job_id, job_id):
                return False
            if self._job.status == "running":
                self._cancel.set()
            return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel.set()
        # A stalled external request may outlive shutdown. It cannot start a new
        # source after the current request completes, and never blocks shutdown.
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=0.25)

    def _run(
        self,
        job_id: str,
        service: SourceAdminService,
        revision: str,
        cancel: threading.Event,
        on_event: Callable[[str, int, int], None] | None,
    ) -> None:
        def notify(state: str, completed: int, total: int) -> None:
            if on_event is not None:
                try:
                    on_event(state, completed, total)
                except Exception:  # noqa: BLE001 - diagnostics must never fail the audit
                    pass

        def progress(completed: int, total: int) -> None:
            with self._lock:
                current = self._job
                if current is not None and current.job_id == job_id and current.status == "running":
                    self._job = AuditJobSnapshot(
                        job_id, "running", completed, total, None, None, None
                    )
            if completed:
                notify("source_completed", completed, total)
            if cancel.is_set():
                raise _AuditJobCancelled
            if time.monotonic() - self._started > JOB_MAX_SECONDS:
                raise _AuditJobDeadline

        notify("started", 0, 0)
        result: tuple[str, str | None, bytes | None, str | None]
        try:
            batch = service.audit_all(on_progress=progress)
            if cancel.is_set():
                raise _AuditJobCancelled
            if time.monotonic() - self._started > JOB_MAX_SECONDS:
                raise _AuditJobDeadline
            archive_bytes = build_source_audit_bundle(batch, build_revision=revision)
            if len(archive_bytes) > JOB_MAX_ARCHIVE_BYTES:
                raise ValueError("Audit result exceeds the bounded archive limit")
            result = (
                "succeeded",
                source_audit_archive_filename(batch.generated_at),
                archive_bytes,
                None,
            )
        except _AuditJobCancelled:
            result = ("cancelled", None, None, "Audit cancelled")
        except _AuditJobDeadline:
            result = ("failed", None, None, "Audit exceeded its time limit")
        except SourceAuditBatchLimitError:
            result = (
                "failed",
                None,
                None,
                f"Source audit export supports at most {MAX_SOURCE_AUDIT_BATCH_SOURCES} sources",
            )
        except Exception:  # noqa: BLE001 - deliberately hide provider and archive failures
            # Never expose provider exception messages, raw bodies, or tokens.
            result = ("failed", None, None, "Audit could not be completed")
        with self._lock:
            current = self._job
            if current is not None and current.job_id == job_id:
                status, filename, payload, error = result
                if cancel.is_set():
                    status, filename, payload, error = "cancelled", None, None, "Audit cancelled"
                self._job = AuditJobSnapshot(
                    job_id, status, current.completed, current.total, filename, payload, error
                )
                self._finished = time.monotonic()
                notify(status, current.completed, current.total)
