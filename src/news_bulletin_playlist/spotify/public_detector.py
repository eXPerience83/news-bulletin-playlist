"""Unauthenticated Spotify public-show change detection for shadow-mode evaluation."""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from html.parser import HTMLParser
from http.client import HTTPException
from types import TracebackType
from typing import Protocol, cast

from news_bulletin_playlist.diagnostics import DiagnosticSeverity, DiagnosticValue
from news_bulletin_playlist.models import SourceDefinition, SourceId
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify.uris import is_spotify_episode_uri

_PUBLIC_SHOW_BASE = "https://open.spotify.com/show"
_EMBED_SHOW_BASE = "https://open.spotify.com/embed/show"
_OEMBED_BASE = "https://open.spotify.com/oembed"
_MAX_RESPONSE_BYTES = 512 * 1024
_DEFAULT_TIMEOUT_SECONDS = 10.0
DETECTOR_SCHEMA_VERSION = 1
_SHOW_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_EPISODE_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


class PublicShowDetectorError(RuntimeError):
    """Base error for the unauthenticated public-show detector."""


class PublicShowConfigurationError(PublicShowDetectorError):
    """Raised when a source cannot identify exactly one supported Spotify show."""


class PublicShowHttpError(PublicShowDetectorError):
    """Raised for a non-successful public Spotify HTTP response."""

    def __init__(self, status: int) -> None:
        super().__init__(f"Spotify public show request returned HTTP {status}")
        self.status = status


class PublicShowTransportError(PublicShowDetectorError):
    """Raised when the public Spotify resource cannot be reached safely."""


class PublicShowResponseError(PublicShowDetectorError):
    """Raised when the public Spotify response no longer satisfies the detector contract."""


@dataclass(frozen=True, slots=True)
class PublicHttpResponse:
    status: int
    content_type: str | None
    body: bytes


class PublicShowTransport(Protocol):
    def get(self, url: str, *, accept: str) -> PublicHttpResponse: ...


@dataclass(frozen=True, slots=True)
class UrllibPublicShowTransport:
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = _MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("public Spotify timeout must be positive")
        if self.max_response_bytes <= 0:
            raise ValueError("public Spotify response limit must be positive")

    def get(self, url: str, *, accept: str) -> PublicHttpResponse:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": accept,
                "User-Agent": "news-bulletin-playlist-public-detector/1",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                status = response.status
                content_type = response.headers.get("Content-Type")
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise PublicShowHttpError(exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
            raise PublicShowTransportError("Spotify public show request failed") from exc

        if status != 200:
            raise PublicShowHttpError(status)
        if len(body) > self.max_response_bytes:
            raise PublicShowResponseError("Spotify public show response exceeded the size limit")
        return PublicHttpResponse(status=status, content_type=content_type, body=body)


@dataclass(frozen=True, slots=True)
class PublicEpisodeObservation:
    source_id: SourceId
    spotify_show_id: str
    episode_id: str
    episode_uri: str | None
    name: str
    title: str
    release_date_raw: str | None
    duration_ms: int | None
    related_entity_uri: str | None
    observed_at: datetime
    detector_schema_version: int = DETECTOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_show_id(self.spotify_show_id)
        _require_episode_id(self.episode_id)
        observed = _as_utc(self.observed_at)
        object.__setattr__(self, "observed_at", observed)
        if self.episode_uri is not None:
            if not is_spotify_episode_uri(self.episode_uri):
                raise ValueError("public Spotify episode URI is invalid")
            if self.episode_uri != f"spotify:episode:{self.episode_id}":
                raise ValueError("public Spotify episode URI does not match episode id")
        if not self.title:
            raise ValueError("public Spotify episode title must not be empty")
        if not self.name:
            raise ValueError("public Spotify episode name must not be empty")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("public Spotify episode duration must not be negative")
        if self.related_entity_uri is not None:
            expected_show_uri = f"spotify:show:{self.spotify_show_id}"
            if self.related_entity_uri != expected_show_uri:
                raise ValueError("public Spotify related show URI does not match source show")

    @property
    def original_title(self) -> str:
        return self.title


@dataclass(frozen=True, slots=True)
class PublicDetectorState:
    source_id: SourceId
    spotify_show_id: str
    last_episode_id: str | None
    last_episode_title: str | None
    last_public_release_date_raw: str | None
    last_public_observed_at: datetime | None
    last_detector_success_at: datetime | None
    last_change_at: datetime | None
    last_failure_at: datetime | None
    last_catalogue_refresh_at: datetime | None
    consecutive_detector_failures: int
    detector_schema_version: int
    last_oembed_title: str | None

    @property
    def inactivity_anchor(self) -> datetime | None:
        candidates = tuple(
            value
            for value in (self.last_change_at, self.last_catalogue_refresh_at)
            if value is not None
        )
        return max(candidates, default=None)

    def audit_due(self, *, now: datetime, inactivity: timedelta) -> bool:
        if inactivity <= timedelta(0):
            raise ValueError("detector inactivity threshold must be positive")
        anchor = self.inactivity_anchor
        if anchor is None:
            return False
        return _as_utc(now) >= anchor + inactivity


class PublicDetectorOutcome(StrEnum):
    BOOTSTRAP = "bootstrap"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    STALE_IGNORED = "stale_ignored"


@dataclass(frozen=True, slots=True)
class PublicDetectorUpdate:
    outcome: PublicDetectorOutcome
    previous: PublicDetectorState | None
    current: PublicDetectorState

    @property
    def changed(self) -> bool:
        return self.outcome in {PublicDetectorOutcome.BOOTSTRAP, PublicDetectorOutcome.CHANGED}


@dataclass(frozen=True, slots=True)
class PublicDetectorCycleSummary:
    observed_count: int
    changed_count: int
    unchanged_count: int
    failure_count: int
    changed_source_ids: tuple[SourceId, ...] = ()


class _NextDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._capturing = False
        self._chunks: list[str] = []
        self.match_count = 0

    @property
    def payload(self) -> str | None:
        if self.match_count != 1:
            return None
        return "".join(self._chunks)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script":
            return
        attributes = dict(attrs)
        if attributes.get("id") != "__NEXT_DATA__":
            return
        self.match_count += 1
        self._capturing = True

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._capturing:
            self._capturing = False


class PublicShowClient:
    """Read the small public Spotify surfaces used only as change signals."""

    def __init__(self, transport: PublicShowTransport | None = None) -> None:
        self.transport = transport or UrllibPublicShowTransport()

    def observe_episode(
        self,
        source_id: SourceId,
        spotify_show_id: str,
        *,
        observed_at: datetime,
    ) -> PublicEpisodeObservation:
        show_id = _require_show_id(spotify_show_id)
        response = self.transport.get(
            f"{_EMBED_SHOW_BASE}/{show_id}",
            accept="text/html,application/xhtml+xml",
        )
        try:
            html = response.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PublicShowResponseError("Spotify Embed response was not UTF-8") from exc
        return parse_public_show_embed(
            html,
            source_id=source_id,
            spotify_show_id=show_id,
            observed_at=observed_at,
        )

    def oembed_title(self, spotify_show_id: str) -> str:
        show_id = _require_show_id(spotify_show_id)
        show_url = f"{_PUBLIC_SHOW_BASE}/{show_id}"
        url = f"{_OEMBED_BASE}?{urllib.parse.urlencode({'url': show_url})}"
        response = self.transport.get(url, accept="application/json")
        try:
            decoded = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicShowResponseError("Spotify oEmbed response was not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise PublicShowResponseError("Spotify oEmbed response was not an object")
        return _bounded_text(decoded.get("title"), "Spotify oEmbed title", max_length=1000)


def parse_public_show_embed(
    html: str,
    *,
    source_id: SourceId,
    spotify_show_id: str,
    observed_at: datetime,
) -> PublicEpisodeObservation:
    """Extract the one episode entity Spotify places in the initial show Embed HTML."""
    parser = _NextDataParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise PublicShowResponseError("Spotify Embed HTML could not be parsed") from exc
    payload = parser.payload
    if payload is None:
        raise PublicShowResponseError("Spotify Embed did not contain exactly one __NEXT_DATA__")

    try:
        root = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PublicShowResponseError("Spotify Embed __NEXT_DATA__ was invalid JSON") from exc

    entity = _object_path(root, ("props", "pageProps", "state", "data", "entity"))
    entity_type = _bounded_text(entity.get("type"), "Spotify Embed entity type", max_length=32)
    if entity_type != "episode":
        raise PublicShowResponseError("Spotify Embed entity was not an episode")

    episode_id = _bounded_text(entity.get("id"), "Spotify episode id", max_length=64)
    _require_episode_id(episode_id)
    episode_uri = _optional_text(entity.get("uri"), "Spotify episode URI", max_length=128)
    name = _bounded_text(entity.get("name"), "Spotify episode name", max_length=1000)
    title = _bounded_text(entity.get("title"), "Spotify episode title", max_length=1000)
    release_date = _optional_text(
        entity.get("releaseDate"),
        "Spotify public releaseDate",
        max_length=128,
    )
    duration = _optional_non_negative_int(entity.get("duration"), "Spotify episode duration")
    related_entity_uri = _optional_text(
        entity.get("relatedEntityUri"),
        "Spotify related entity URI",
        max_length=128,
    )
    try:
        return PublicEpisodeObservation(
            source_id=source_id,
            spotify_show_id=spotify_show_id,
            episode_id=episode_id,
            episode_uri=episode_uri,
            name=name,
            title=title,
            release_date_raw=release_date,
            duration_ms=duration,
            related_entity_uri=related_entity_uri,
            observed_at=observed_at,
        )
    except ValueError as exc:
        raise PublicShowResponseError("Spotify Embed episode entity failed validation") from exc


def spotify_show_id(source: SourceDefinition) -> str:
    show_ids = {
        reference.external_id.strip()
        for reference in source.external_references
        if reference.system.casefold() == "spotify"
        and reference.resource_type.casefold() == "show"
        and reference.external_id.strip()
    }
    if len(show_ids) != 1:
        raise PublicShowConfigurationError(
            f"source {source.id} must declare exactly one Spotify show"
        )
    try:
        return _require_show_id(next(iter(show_ids)))
    except ValueError as exc:
        raise PublicShowConfigurationError(
            f"source {source.id} has an invalid Spotify show id"
        ) from exc


_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS spotify_public_detector_state (
    source_id TEXT NOT NULL,
    spotify_show_id TEXT NOT NULL,
    last_episode_id TEXT,
    last_episode_title TEXT,
    last_public_release_date_raw TEXT,
    last_public_observed_at TEXT,
    last_detector_success_at TEXT,
    last_change_at TEXT,
    last_failure_at TEXT,
    last_catalogue_refresh_at TEXT,
    consecutive_detector_failures INTEGER NOT NULL DEFAULT 0
        CHECK (consecutive_detector_failures >= 0),
    detector_schema_version INTEGER NOT NULL,
    last_oembed_title TEXT,
    PRIMARY KEY (source_id, spotify_show_id)
)
"""


class _ConnectionContext:
    def __init__(self, connection: sqlite3.Connection, operation: str, path: str) -> None:
        self.connection = connection
        self.operation = operation
        self.path = path

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del traceback
        db_error = exc_value if isinstance(exc_value, sqlite3.Error) else None
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        except sqlite3.Error as error:
            if db_error is None:
                db_error = error
        finally:
            try:
                self.connection.close()
            except sqlite3.Error as error:
                if db_error is None:
                    db_error = error
        if exc_value is not None and not isinstance(exc_value, sqlite3.Error):
            return
        if db_error is not None:
            raise PersistenceError(f"{self.operation} failed for {self.path}") from db_error


class PublicDetectorJournal:
    """Durable state for the public show detector, independent from legacy RSS matching."""

    def __init__(self, store: SQLiteStore) -> None:
        self.path = store.path

    def initialize(self) -> None:
        with self._connection("initialize Spotify public detector") as connection:
            connection.execute(_TABLE_SQL)

    def get(self, source_id: SourceId, spotify_show_id: str) -> PublicDetectorState | None:
        show_id = _require_show_id(spotify_show_id)
        with self._connection("read Spotify public detector state") as connection:
            self._ensure_table(connection)
            row = self._select(connection, source_id, show_id)
        return None if row is None else _state_from_row(row)

    def record_success(
        self,
        observation: PublicEpisodeObservation,
        *,
        oembed_title: str | None = None,
    ) -> PublicDetectorUpdate:
        with self._connection("persist Spotify public detector success") as connection:
            self._ensure_table(connection)
            row = self._select(connection, observation.source_id, observation.spotify_show_id)
            previous = None if row is None else _state_from_row(row)
            if (
                previous is not None
                and previous.last_public_observed_at is not None
                and observation.observed_at < previous.last_public_observed_at
            ):
                return PublicDetectorUpdate(
                    PublicDetectorOutcome.STALE_IGNORED,
                    previous,
                    previous,
                )

            if previous is None or previous.last_episode_id is None:
                outcome = PublicDetectorOutcome.BOOTSTRAP
            elif previous.last_episode_id != observation.episode_id:
                outcome = PublicDetectorOutcome.CHANGED
            else:
                outcome = PublicDetectorOutcome.UNCHANGED

            last_change_at: datetime | None
            if outcome in {
                PublicDetectorOutcome.BOOTSTRAP,
                PublicDetectorOutcome.CHANGED,
            }:
                last_change_at = observation.observed_at
            else:
                assert previous is not None
                last_change_at = previous.last_change_at
            last_failure_at = None if previous is None else previous.last_failure_at
            last_catalogue_refresh_at = (
                None if previous is None else previous.last_catalogue_refresh_at
            )
            stored_oembed_title = (
                oembed_title
                if oembed_title is not None
                else (None if previous is None else previous.last_oembed_title)
            )
            connection.execute(
                """
                INSERT INTO spotify_public_detector_state (
                    source_id, spotify_show_id, last_episode_id, last_episode_title,
                    last_public_release_date_raw, last_public_observed_at,
                    last_detector_success_at, last_change_at, last_failure_at,
                    last_catalogue_refresh_at, consecutive_detector_failures,
                    detector_schema_version, last_oembed_title
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(source_id, spotify_show_id) DO UPDATE SET
                    last_episode_id = excluded.last_episode_id,
                    last_episode_title = excluded.last_episode_title,
                    last_public_release_date_raw = excluded.last_public_release_date_raw,
                    last_public_observed_at = excluded.last_public_observed_at,
                    last_detector_success_at = excluded.last_detector_success_at,
                    last_change_at = excluded.last_change_at,
                    last_failure_at = excluded.last_failure_at,
                    last_catalogue_refresh_at = excluded.last_catalogue_refresh_at,
                    consecutive_detector_failures = 0,
                    detector_schema_version = excluded.detector_schema_version,
                    last_oembed_title = excluded.last_oembed_title
                """,
                (
                    str(observation.source_id),
                    observation.spotify_show_id,
                    observation.episode_id,
                    observation.original_title,
                    observation.release_date_raw,
                    _timestamp(observation.observed_at),
                    _timestamp(observation.observed_at),
                    _optional_timestamp(last_change_at),
                    _optional_timestamp(last_failure_at),
                    _optional_timestamp(last_catalogue_refresh_at),
                    observation.detector_schema_version,
                    stored_oembed_title,
                ),
            )
            current_row = self._select(
                connection,
                observation.source_id,
                observation.spotify_show_id,
            )
            if current_row is None:
                raise PersistenceError("Spotify public detector state disappeared after update")
            current = _state_from_row(current_row)
        return PublicDetectorUpdate(outcome, previous, current)

    def record_failure(
        self,
        source_id: SourceId,
        spotify_show_id: str,
        *,
        failed_at: datetime,
    ) -> PublicDetectorState:
        show_id = _require_show_id(spotify_show_id)
        failed = _as_utc(failed_at)
        with self._connection("persist Spotify public detector failure") as connection:
            self._ensure_table(connection)
            row = self._select(connection, source_id, show_id)
            previous = None if row is None else _state_from_row(row)
            latest_known: datetime | None = None
            if previous is not None:
                known_timestamps = tuple(
                    value
                    for value in (
                        previous.last_failure_at,
                        previous.last_detector_success_at,
                    )
                    if value is not None
                )
                latest_known = max(known_timestamps, default=None)
            if latest_known is not None and failed < latest_known:
                assert previous is not None
                return previous
            failures = 1 if previous is None else previous.consecutive_detector_failures + 1
            connection.execute(
                """
                INSERT INTO spotify_public_detector_state (
                    source_id, spotify_show_id, last_failure_at,
                    consecutive_detector_failures, detector_schema_version
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_id, spotify_show_id) DO UPDATE SET
                    last_failure_at = excluded.last_failure_at,
                    consecutive_detector_failures = excluded.consecutive_detector_failures,
                    detector_schema_version = excluded.detector_schema_version
                """,
                (
                    str(source_id),
                    show_id,
                    _timestamp(failed),
                    failures,
                    DETECTOR_SCHEMA_VERSION,
                ),
            )
            current_row = self._select(connection, source_id, show_id)
            if current_row is None:
                raise PersistenceError("Spotify public detector failure state was not persisted")
            return _state_from_row(current_row)

    def mark_catalogue_refresh(
        self,
        source_id: SourceId,
        spotify_show_id: str,
        *,
        refreshed_at: datetime,
    ) -> bool:
        show_id = _require_show_id(spotify_show_id)
        refreshed = _timestamp(refreshed_at)
        with self._connection("persist Spotify public catalogue refresh") as connection:
            self._ensure_table(connection)
            cursor = connection.execute(
                """
                UPDATE spotify_public_detector_state
                SET last_catalogue_refresh_at = ?
                WHERE source_id = ? AND spotify_show_id = ?
                  AND (
                      last_catalogue_refresh_at IS NULL
                      OR last_catalogue_refresh_at <= ?
                  )
                """,
                (refreshed, str(source_id), show_id, refreshed),
            )
            return cursor.rowcount > 0

    @staticmethod
    def _select(
        connection: sqlite3.Connection,
        source_id: SourceId,
        spotify_show_id: str,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            """
            SELECT source_id, spotify_show_id, last_episode_id, last_episode_title,
                   last_public_release_date_raw, last_public_observed_at,
                   last_detector_success_at, last_change_at, last_failure_at,
                   last_catalogue_refresh_at, consecutive_detector_failures,
                   detector_schema_version, last_oembed_title
            FROM spotify_public_detector_state
            WHERE source_id = ? AND spotify_show_id = ?
            """,
            (str(source_id), spotify_show_id),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    @staticmethod
    def _ensure_table(connection: sqlite3.Connection) -> None:
        connection.execute(_TABLE_SQL)

    def _connection(self, operation: str) -> _ConnectionContext:
        try:
            connection = sqlite3.connect(self.path, timeout=10.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error as exc:
            raise PersistenceError(f"{operation} failed for {self.path}: {exc}") from exc
        return _ConnectionContext(connection, operation, str(self.path))


class PublicDetectorShadow:
    """Observe public Spotify state without changing authoritative engine behavior."""

    def __init__(
        self,
        journal: PublicDetectorJournal,
        diagnostics: OperationalDiagnostics,
        *,
        client: PublicShowClient | None = None,
    ) -> None:
        self.journal = journal
        self.diagnostics = diagnostics
        self.client = client or PublicShowClient()

    def observe_sources(
        self,
        sources: Iterable[SourceDefinition],
        *,
        observed_at: datetime,
    ) -> PublicDetectorCycleSummary:
        observed = _as_utc(observed_at)
        changed_count = 0
        unchanged_count = 0
        failure_count = 0
        observed_count = 0
        changed_source_ids: list[SourceId] = []

        for source in sources:
            if not source.enabled:
                continue
            try:
                show_id = spotify_show_id(source)
            except PublicShowConfigurationError:
                failure_count += 1
                self._emit_failure(
                    source.id,
                    "configuration_failure",
                    observed_at=observed,
                )
                continue

            try:
                previous = self.journal.get(source.id, show_id)
                observation = self.client.observe_episode(
                    source.id,
                    show_id,
                    observed_at=observed,
                )
            except PublicShowHttpError as exc:
                failure_count += 1
                self._record_failure(
                    source.id,
                    show_id,
                    observed,
                    outcome="http_failure",
                    http_status=exc.status,
                )
                continue
            except PublicShowTransportError:
                failure_count += 1
                self._record_failure(
                    source.id,
                    show_id,
                    observed,
                    outcome="transport_failure",
                )
                continue
            except PublicShowResponseError:
                failure_count += 1
                self._record_failure(
                    source.id,
                    show_id,
                    observed,
                    outcome="response_failure",
                )
                continue
            except PersistenceError:
                failure_count += 1
                self._emit_failure(source.id, "persistence_failure", observed_at=observed)
                continue

            candidate_changed = (
                previous is None or previous.last_episode_id != observation.episode_id
            )
            oembed_title: str | None = None
            if candidate_changed:
                try:
                    oembed_title = self.client.oembed_title(show_id)
                except PublicShowDetectorError:
                    self._emit_failure(
                        source.id,
                        "oembed_failure",
                        observed_at=observed,
                    )

            try:
                update = self.journal.record_success(
                    observation,
                    oembed_title=oembed_title,
                )
            except PersistenceError:
                failure_count += 1
                self._emit_failure(source.id, "persistence_failure", observed_at=observed)
                continue

            observed_count += 1
            recovered_failures = 0 if previous is None else previous.consecutive_detector_failures
            if update.outcome in {PublicDetectorOutcome.BOOTSTRAP, PublicDetectorOutcome.CHANGED}:
                changed_count += 1
                changed_source_ids.append(source.id)
                self._emit_change(update, observation, oembed_title=oembed_title)
            elif update.outcome is PublicDetectorOutcome.UNCHANGED:
                unchanged_count += 1
            else:
                self.diagnostics.emit(
                    occurred_at=observed,
                    severity=DiagnosticSeverity.WARNING,
                    component="spotify.public_detector",
                    event_name="public_detector_stale_ignored",
                    source_id=str(source.id),
                    details={
                        "detector_outcome": "stale_ignored",
                        "spotify_episode_id": observation.episode_id,
                        "detector_schema_version": DETECTOR_SCHEMA_VERSION,
                    },
                )
            if recovered_failures:
                self.diagnostics.emit(
                    occurred_at=observed,
                    severity=DiagnosticSeverity.INFO,
                    component="spotify.public_detector",
                    event_name="public_detector_recovered",
                    source_id=str(source.id),
                    details={
                        "detector_outcome": "recovered",
                        "consecutive_failure_count": recovered_failures,
                        "spotify_episode_id": observation.episode_id,
                    },
                )

        self.diagnostics.emit(
            occurred_at=observed,
            severity=DiagnosticSeverity.INFO,
            component="spotify.public_detector",
            event_name="public_detector_cycle_completed",
            details={
                "detector_source_count": observed_count + failure_count,
                "detector_changed_count": changed_count,
                "detector_unchanged_count": unchanged_count,
                "detector_failure_count": failure_count,
            },
        )
        return PublicDetectorCycleSummary(
            observed_count=observed_count,
            changed_count=changed_count,
            unchanged_count=unchanged_count,
            failure_count=failure_count,
            changed_source_ids=tuple(changed_source_ids),
        )

    def _record_failure(
        self,
        source_id: SourceId,
        show_id: str,
        observed_at: datetime,
        *,
        outcome: str,
        http_status: int | None = None,
    ) -> None:
        try:
            state = self.journal.record_failure(
                source_id,
                show_id,
                failed_at=observed_at,
            )
        except PersistenceError:
            self._emit_failure(source_id, "persistence_failure", observed_at=observed_at)
            return
        details: dict[str, DiagnosticValue] = {
            "detector_outcome": outcome,
            "consecutive_failure_count": state.consecutive_detector_failures,
        }
        if http_status is not None:
            details["http_status"] = http_status
        self.diagnostics.emit(
            occurred_at=observed_at,
            severity=DiagnosticSeverity.WARNING,
            component="spotify.public_detector",
            event_name="public_detector_failed",
            source_id=str(source_id),
            details=details,
        )

    def _emit_failure(
        self,
        source_id: SourceId,
        outcome: str,
        *,
        observed_at: datetime,
    ) -> None:
        self.diagnostics.emit(
            occurred_at=observed_at,
            severity=DiagnosticSeverity.WARNING,
            component="spotify.public_detector",
            event_name=(
                "public_detector_oembed_failed"
                if outcome == "oembed_failure"
                else "public_detector_failed"
            ),
            source_id=str(source_id),
            details={"detector_outcome": outcome},
        )

    def _emit_change(
        self,
        update: PublicDetectorUpdate,
        observation: PublicEpisodeObservation,
        *,
        oembed_title: str | None,
    ) -> None:
        previous = update.previous
        oembed_matches = (
            None if oembed_title is None else oembed_title in {observation.title, observation.name}
        )
        details: dict[str, DiagnosticValue] = {
            "detector_outcome": update.outcome.value,
            "spotify_episode_id": observation.episode_id,
            "spotify_previous_episode_id": (None if previous is None else previous.last_episode_id),
            "spotify_episode_title": observation.original_title,
            "spotify_previous_episode_title": (
                None if previous is None else previous.last_episode_title
            ),
            "spotify_oembed_title": oembed_title,
            "oembed_matches": oembed_matches,
            "public_release_date": observation.release_date_raw,
            "detector_schema_version": observation.detector_schema_version,
        }
        self.diagnostics.emit(
            occurred_at=observation.observed_at,
            severity=(
                DiagnosticSeverity.WARNING if oembed_matches is False else DiagnosticSeverity.INFO
            ),
            component="spotify.public_detector",
            event_name="public_detector_changed",
            source_id=str(observation.source_id),
            details=details,
        )


def _object_path(root: object, path: tuple[str, ...]) -> dict[str, object]:
    value = root
    for key in path:
        if not isinstance(value, dict):
            raise PublicShowResponseError("Spotify Embed __NEXT_DATA__ shape changed")
        value = value.get(key)
    if not isinstance(value, dict):
        raise PublicShowResponseError("Spotify Embed episode entity was missing")
    return cast(dict[str, object], value)


def _bounded_text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise PublicShowResponseError(f"{label} was missing or invalid")
    return value


def _optional_text(value: object, label: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise PublicShowResponseError(f"{label} was invalid")
    return value


def _optional_non_negative_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicShowResponseError(f"{label} was invalid")
    return value


def _require_show_id(value: str) -> str:
    if not _SHOW_ID_RE.fullmatch(value):
        raise ValueError("Spotify show id must be a 22-character base62 identifier")
    return value


def _require_episode_id(value: str) -> str:
    if not _EPISODE_ID_RE.fullmatch(value):
        raise ValueError("Spotify episode id must be a 22-character base62 identifier")
    return value


def _state_from_row(row: sqlite3.Row) -> PublicDetectorState:
    failures = row["consecutive_detector_failures"]
    schema_version = row["detector_schema_version"]
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise PersistenceError("Spotify public detector stored an invalid failure count")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise PersistenceError("Spotify public detector stored an invalid schema version")
    source_id = _stored_text(row["source_id"], "source_id")
    show_id = _stored_text(row["spotify_show_id"], "spotify_show_id")
    try:
        _require_show_id(show_id)
    except ValueError as exc:
        raise PersistenceError("Spotify public detector stored an invalid show id") from exc
    episode_id = _stored_optional_text(row["last_episode_id"], "last_episode_id")
    if episode_id is not None:
        try:
            _require_episode_id(episode_id)
        except ValueError as exc:
            raise PersistenceError("Spotify public detector stored an invalid episode id") from exc
    return PublicDetectorState(
        source_id=SourceId(source_id),
        spotify_show_id=show_id,
        last_episode_id=episode_id,
        last_episode_title=_stored_optional_text(
            row["last_episode_title"],
            "last_episode_title",
        ),
        last_public_release_date_raw=_stored_optional_text(
            row["last_public_release_date_raw"],
            "last_public_release_date_raw",
        ),
        last_public_observed_at=_stored_optional_timestamp(row["last_public_observed_at"]),
        last_detector_success_at=_stored_optional_timestamp(row["last_detector_success_at"]),
        last_change_at=_stored_optional_timestamp(row["last_change_at"]),
        last_failure_at=_stored_optional_timestamp(row["last_failure_at"]),
        last_catalogue_refresh_at=_stored_optional_timestamp(row["last_catalogue_refresh_at"]),
        consecutive_detector_failures=failures,
        detector_schema_version=schema_version,
        last_oembed_title=_stored_optional_text(row["last_oembed_title"], "last_oembed_title"),
    )


def _stored_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PersistenceError(f"Spotify public detector stored non-text {label}")
    return value


def _stored_optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _stored_text(value, label)


def _stored_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersistenceError("Spotify public detector stored a non-text timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersistenceError("Spotify public detector stored an invalid timestamp") from exc
    return _as_utc(parsed)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _optional_timestamp(value: datetime | None) -> str | None:
    return None if value is None else _timestamp(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Spotify public detector timestamps must be timezone-aware")
    return value.astimezone(UTC)
