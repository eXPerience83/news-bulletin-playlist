"""Bounded in-memory administration sessions and session-scoped CSRF state."""

from __future__ import annotations

import hmac
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from threading import RLock
from typing import Any
from weakref import WeakKeyDictionary

from news_bulletin_playlist.portable_sources import MAX_SOURCE_DOCUMENT_BYTES

ADMIN_SESSION_COOKIE = "news_playlist_admin_session"
ADMIN_SESSION_IDLE_TTL = timedelta(minutes=30)
ADMIN_SESSION_ABSOLUTE_TTL = timedelta(hours=12)
ADMIN_SESSION_CSRF_TTL = timedelta(minutes=10)
ADMIN_SESSION_COOKIE_MAX_AGE = int(ADMIN_SESSION_ABSOLUTE_TTL.total_seconds())
SOURCE_IMPORT_PREVIEW_TTL = timedelta(minutes=10)
MAX_SOURCE_IMPORT_PREVIEWS = 8
_LOGIN_FAILURE_WINDOW = timedelta(minutes=1)
_LOGIN_FAILURE_DELAY_STEP_SECONDS = 0.1
_LOGIN_FAILURE_DELAY_MAX_SECONDS = 0.5

_CURRENT_ADMIN_CSRF_TOKEN: ContextVar[str | None] = ContextVar(
    "news_playlist_admin_csrf_token",
    default=None,
)


@dataclass(slots=True)
class _AdminSession:
    created_at: datetime
    last_seen_at: datetime
    csrf_token: str | None = None
    csrf_expires_at: datetime | None = None
    source_inspection: SourceInspectionContext | None = None
    source_import: SourceImportContext | None = None


@dataclass(frozen=True, slots=True)
class SourceImportContext:
    """One bounded upload bound to a session, document and configuration snapshot."""

    document: bytes
    fingerprint: str
    preview_id: str
    expires_at: datetime


class SourceImportCapacityError(ValueError):
    """The bounded source-preview capacity is exhausted; the operator may retry."""


@dataclass(frozen=True, slots=True)
class SourceInspectionContext:
    """Normalized, ephemeral review binding; never an authorization to persist."""

    spotify_show_id: str
    parser_id: str
    collection_filter_id: str | None
    report_json: bytes
    purpose: str = "candidate"


class AdminSessionManager:
    """Keep opaque bounded sessions for one configured administration credential set."""

    def __init__(self) -> None:
        self._sessions: dict[str, _AdminSession] = {}
        self._login_failures: dict[str, tuple[datetime, int]] = {}
        self._lock = RLock()

    def create_session(self, *, now: datetime | None = None) -> str:
        observed_at = _as_utc(now or datetime.now(UTC))
        with self._lock:
            self._prune_sessions_locked(observed_at)
            while True:
                session_id = secrets.token_urlsafe(32)
                if session_id not in self._sessions:
                    break
            self._sessions[session_id] = _AdminSession(
                created_at=observed_at,
                last_seen_at=observed_at,
            )
            return session_id

    def resolve_session(
        self,
        cookie_header: str | None,
        *,
        now: datetime | None = None,
    ) -> str | None:
        session_id = _session_id_from_cookie(cookie_header)
        if session_id is None:
            return None
        observed_at = _as_utc(now or datetime.now(UTC))
        with self._lock:
            self._prune_sessions_locked(observed_at)
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.last_seen_at = observed_at
            return session_id

    def invalidate_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def source_inspection(self, session_id: str) -> SourceInspectionContext | None:
        with self._lock:
            session = self._sessions.get(session_id)
            return None if session is None else session.source_inspection

    def set_source_inspection(
        self, session_id: str, context: SourceInspectionContext | None
    ) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise RuntimeError("administration session is unavailable")
            session.source_inspection = context

    def invalidate_cookie_session(self, cookie_header: str | None) -> None:
        session_id = _session_id_from_cookie(cookie_header)
        if session_id is not None:
            self.invalidate_session(session_id)

    def stage_source_import(
        self,
        session_id: str,
        document: bytes,
        fingerprint: str,
        *,
        now: datetime | None = None,
    ) -> SourceImportContext:
        if not isinstance(document, bytes) or not 0 < len(document) <= MAX_SOURCE_DOCUMENT_BYTES:
            raise ValueError("Invalid source import document size")
        observed_at = _as_utc(now or datetime.now(UTC))
        with self._lock:
            self._prune_sessions_locked(observed_at)
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError("Source import session is unavailable")
            staged = sum(item.source_import is not None for item in self._sessions.values())
            if session.source_import is None and staged >= MAX_SOURCE_IMPORT_PREVIEWS:
                raise SourceImportCapacityError(
                    "Too many source previews; cancel an existing preview or try later"
                )
            context = SourceImportContext(
                document,
                fingerprint,
                secrets.token_urlsafe(32),
                observed_at + SOURCE_IMPORT_PREVIEW_TTL,
            )
            session.source_import = context
            return context

    def take_source_import(
        self,
        session_id: str,
        preview_id: str,
        *,
        now: datetime | None = None,
    ) -> SourceImportContext | None:
        observed_at = _as_utc(now or datetime.now(UTC))
        with self._lock:
            self._prune_sessions_locked(observed_at)
            session = self._sessions.get(session_id)
            context = None if session is None else session.source_import
            if context is None or not hmac.compare_digest(
                context.preview_id.encode("utf-8"), preview_id.encode("utf-8")
            ):
                return None
            assert session is not None
            session.source_import = None
            return context

    def issue_csrf_token(
        self,
        session_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        observed_at = _as_utc(now or datetime.now(UTC))
        with self._lock:
            self._prune_sessions_locked(observed_at)
            session = self._sessions.get(session_id)
            if session is None:
                raise RuntimeError("administration session is unavailable")
            if (
                session.csrf_token is not None
                and session.csrf_expires_at is not None
                and observed_at < session.csrf_expires_at
            ):
                return session.csrf_token
            token = secrets.token_urlsafe(32)
            session.csrf_token = token
            session.csrf_expires_at = observed_at + ADMIN_SESSION_CSRF_TTL
            return token

    def consume_csrf_token(
        self,
        session_id: str,
        token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        observed_at = _as_utc(now or datetime.now(UTC))
        with self._lock:
            self._prune_sessions_locked(observed_at)
            session = self._sessions.get(session_id)
            if session is None:
                return False
            expected = session.csrf_token
            expires_at = session.csrf_expires_at
            if expected is None or expires_at is None:
                return False
            if observed_at >= expires_at or not hmac.compare_digest(token, expected):
                return False
            session.csrf_token = None
            session.csrf_expires_at = None
            return True

    def invalid_login_delay(self, client_id: str, *, now: datetime | None = None) -> float:
        """Record one invalid login and return a small bounded response delay."""
        observed_at = _as_utc(now or datetime.now(UTC))
        cutoff = observed_at - _LOGIN_FAILURE_WINDOW
        with self._lock:
            self._login_failures = {
                identity: (last_failure, count)
                for identity, (last_failure, count) in self._login_failures.items()
                if last_failure >= cutoff
            }
            _last_failure, previous_count = self._login_failures.get(client_id, (observed_at, 0))
            count = min(previous_count + 1, 7)
            self._login_failures[client_id] = (observed_at, count)
            penalty_steps = max(0, count - 2)
            return min(
                penalty_steps * _LOGIN_FAILURE_DELAY_STEP_SECONDS,
                _LOGIN_FAILURE_DELAY_MAX_SECONDS,
            )

    def clear_login_failures(self, client_id: str) -> None:
        with self._lock:
            self._login_failures.pop(client_id, None)

    def _prune_sessions_locked(self, observed_at: datetime) -> None:
        for session in self._sessions.values():
            if (
                session.source_import is not None
                and observed_at >= session.source_import.expires_at
            ):
                session.source_import = None
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if (
                observed_at >= session.created_at + ADMIN_SESSION_ABSOLUTE_TTL
                or observed_at >= session.last_seen_at + ADMIN_SESSION_IDLE_TTL
            )
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)


_MANAGERS: WeakKeyDictionary[Any, AdminSessionManager] = WeakKeyDictionary()
_MANAGERS_LOCK = RLock()


def admin_session_manager_for(security: object) -> AdminSessionManager:
    """Return the session manager whose lifetime follows one credential object."""
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(security)
        if manager is None:
            manager = AdminSessionManager()
            _MANAGERS[security] = manager
        return manager


def session_cookie_header(session_id: str, *, secure: bool) -> str:
    attributes = [
        f"{ADMIN_SESSION_COOKIE}={session_id}",
        "Path=/admin",
        f"Max-Age={ADMIN_SESSION_COOKIE_MAX_AGE}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


def expired_session_cookie_header(*, secure: bool) -> str:
    attributes = [
        f"{ADMIN_SESSION_COOKIE}=",
        "Path=/admin",
        "Max-Age=0",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


def set_current_admin_csrf_token(token: str | None) -> None:
    """Expose the authenticated request's logout token to the shared server renderer."""
    _CURRENT_ADMIN_CSRF_TOKEN.set(token)


def current_admin_csrf_token() -> str | None:
    return _CURRENT_ADMIN_CSRF_TOKEN.get()


def _session_id_from_cookie(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except CookieError:
        return None
    morsel = cookie.get(ADMIN_SESSION_COOKIE)
    if morsel is None:
        return None
    candidate = morsel.value
    if not 20 <= len(candidate) <= 256:
        return None
    return candidate


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)
