"""Concise stdout logging backed by the bounded durable diagnostic event store."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime

from news_bulletin_playlist.diagnostics import (
    DiagnosticEventStore,
    DiagnosticSeverity,
    DiagnosticValue,
)
from news_bulletin_playlist.persistence import PersistenceError

_LOGGER_NAME = "news_bulletin_playlist"
_HANDLER_MARKER = "_news_bulletin_operational_handler"


def _utc_converter(timestamp: float | None) -> time.struct_time:
    return time.gmtime(timestamp)


class _UtcFormatter(logging.Formatter):
    converter = staticmethod(_utc_converter)


def configure_operational_logging() -> logging.Logger:
    """Return the one process logger used for concise production operational output."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(handler, _HANDLER_MARKER, False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(
            _UtcFormatter(
                fmt="%(asctime)sZ level=%(levelname)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(handler)
    return logger


def cycle_id_for(value: datetime) -> str:
    """Create a stable, non-secret cycle correlation id from a UTC timestamp."""
    observed = _as_utc(value)
    return f"cycle-{observed.strftime('%Y%m%dT%H%M%S%fZ')}"


class OperationalDiagnostics:
    """Emit one sanitized operational event to stdout and, when available, SQLite."""

    def __init__(
        self,
        store: DiagnosticEventStore | None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.logger = configure_operational_logging() if logger is None else logger

    def emit(
        self,
        *,
        occurred_at: datetime,
        severity: DiagnosticSeverity,
        component: str,
        event_name: str,
        cycle_id: str | None = None,
        source_id: str | None = None,
        playlist_id: str | None = None,
        details: Mapping[str, DiagnosticValue] | None = None,
    ) -> None:
        """Persist first, then log only the same allow-listed fields.

        Observability must never make the engine unavailable. Persistence or validation
        failures therefore degrade to one fixed, non-secret logger event instead of
        propagating arbitrary exception text.
        """
        normalized_details = dict(details or {})
        if self.store is not None:
            try:
                self.store.record(
                    occurred_at=occurred_at,
                    severity=severity,
                    component=component,
                    event_name=event_name,
                    cycle_id=cycle_id,
                    source_id=source_id,
                    playlist_id=playlist_id,
                    details=normalized_details,
                )
            except (PersistenceError, ValueError):
                self.logger.error(
                    "event=diagnostic_persistence_failed component=diagnostics"
                )
                return

        fields = [
            f"event={event_name}",
            f"component={component}",
        ]
        if cycle_id is not None:
            fields.append(f"cycle={cycle_id}")
        if source_id is not None:
            fields.append(f"source={source_id}")
        if playlist_id is not None:
            fields.append(f"playlist={playlist_id}")
        for key in sorted(normalized_details):
            fields.append(f"{key}={_render_value(normalized_details[key])}")

        message = " ".join(fields)
        if severity is DiagnosticSeverity.ERROR:
            self.logger.error(message)
        elif severity is DiagnosticSeverity.WARNING:
            self.logger.warning(message)
        else:
            self.logger.info(message)


def _render_value(value: DiagnosticValue) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("operational diagnostic timestamps must be timezone-aware")
    return value.astimezone(UTC)
