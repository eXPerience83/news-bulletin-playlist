"""Compose authenticated diagnostics into the production runtime without provider raw data."""

from __future__ import annotations

import html
import os
import re
import threading
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from http import HTTPStatus
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from news_bulletin_playlist import __version__, engine_runtime
from news_bulletin_playlist.admin_session import SourceImportCapacityError, SourceInspectionContext
from news_bulletin_playlist.admin_web import render_admin_shell
from news_bulletin_playlist.build_info import build_revision as resolve_build_revision
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.custom_cover_assets import CustomCoverAssetError
from news_bulletin_playlist.diagnostics import (
    MAX_DIAGNOSTIC_EXPORT_EVENTS,
    DiagnosticEvent,
    DiagnosticSeverity,
)
from news_bulletin_playlist.diagnostics_web import (
    DiagnosticFilters,
    build_diagnostic_bundle,
    parse_diagnostic_filters,
    render_diagnostics_page,
)
from news_bulletin_playlist.engine import EngineCycleResult, OperationalStatus
from news_bulletin_playlist.managed_admin import ManagedAdminError, ManagedAdminService
from news_bulletin_playlist.managed_admin_web import (
    duration_minutes_from_form,
    playlist_id_from_form,
    single_form_value,
)
from news_bulletin_playlist.managed_provisioning import (
    MANAGED_PROVISIONING_FILENAME,
    ProvisioningJournal,
    ProvisioningJournalError,
)
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedStateError,
    ManagedStateStore,
    effective_sources,
)
from news_bulletin_playlist.models import ParserId
from news_bulletin_playlist.persistence import PersistenceError
from news_bulletin_playlist.portable_playlists import (
    PlaylistExportError,
    PlaylistExportService,
    PortableCoverLoader,
)
from news_bulletin_playlist.portable_playlists_web import render_identity_confirmation
from news_bulletin_playlist.portable_sources import PortableSourceError, UnsupportedSourceCapability
from news_bulletin_playlist.runtime import (
    DEFAULT_DATA_DIR,
    DEFAULT_HEALTH_HOST,
    DEFAULT_HEALTH_PORT,
    _data_dir_ready,
)
from news_bulletin_playlist.source_admin import (
    EngineCycleBusyError,
    NoSourcesToExportError,
    SourceDisableError,
    SourceMutationError,
    SourceReferencedError,
    export_source_configuration,
)
from news_bulletin_playlist.source_admin_web import (
    render_new_source_page,
    render_source_review_page,
)
from news_bulletin_playlist.source_audit_jobs import AuditJobBusyError, SourceAuditJobs
from news_bulletin_playlist.source_import import (
    SourceImportError,
    SourceImportService,
    SourceImportWriteError,
    StaleSourceImport,
)
from news_bulletin_playlist.source_import_web import (
    MAX_SOURCE_IMPORT_MULTIPART_BYTES,
    parse_source_import_upload,
    render_source_import_preview,
    render_source_import_result,
    render_source_import_upload,
)
from news_bulletin_playlist.source_inspection import (
    InspectionFailure,
    SourceInspectionError,
    serialize_source_inspection,
)
from news_bulletin_playlist.source_intake import SourceIntakeError, SourceIntakeFailure
from news_bulletin_playlist.sources_admin_web import (
    render_builtin_public_release_page,
    render_delete_source_page,
    render_edit_source_page,
    render_sources_page,
)
from news_bulletin_playlist.spotify.auth import AuthorizationState
from news_bulletin_playlist.spotify.authorization_display import authorization_state_label
from news_bulletin_playlist.spotify_metrics import (
    SpotifyRequestMetrics,
    SpotifyRequestMetricsSnapshot,
)
from news_bulletin_playlist.web_ui import WEB_UI_STYLES, BadgeTone, status_badge


class DiagnosticOperationalHealthHandler(engine_runtime.OperationalHealthHandler):
    """Add private diagnostic routes and keep detailed failures off the public page."""

    diagnostic_timezone: tzinfo = UTC
    diagnostic_timezone_label = "UTC"
    build_revision = "dev"
    source_audit_jobs: SourceAuditJobs | None = None
    spotify_request_metrics: SpotifyRequestMetrics | None = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, b"Invalid request")
            return

        if parsed.path == "/":
            self._serve_safe_public_status()
            return
        if parsed.path in {"/admin/playlists/export.zip", "/admin/playlists/portable/prepare"}:
            self._serve_portable_playlist(parsed.path, parsed.query)
            return
        if parsed.path == "/admin/sources":
            self._serve_sources(parsed.query)
            return
        if parsed.path == "/admin/sources/export.json":
            self._serve_source_configuration(parsed.query)
            return
        if parsed.path == "/admin/sources/import":
            security = self._require_admin()
            if security is not None:
                if parsed.query or parsed.fragment:
                    self._source_error(HTTPStatus.BAD_REQUEST, "Invalid import request")
                else:
                    self._reply(
                        HTTPStatus.OK,
                        render_source_import_upload(
                            csrf_token=security.issue_csrf_token(),
                            lan_mode=self._is_lan_admin_mode(),
                        ),
                    )
            return
        if parsed.path == "/admin/sources/new":
            self._serve_source_new()
            return
        if parsed.path == "/admin/sources/edit":
            self._serve_source_edit(parsed.query)
            return
        if parsed.path == "/admin/sources/delete":
            self._serve_source_delete(parsed.query)
            return
        if parsed.path == "/admin/sources/audits/status":
            self._serve_audit_job(parsed.query, download=False)
            return
        if parsed.path == "/admin/sources/audits/download":
            self._serve_audit_job(parsed.query, download=True)
            return
        if parsed.path == "/admin/sources/inspection-report.json":
            self._serve_source_report()
            return
        if parsed.path == "/admin/diagnostics":
            self._serve_diagnostics(parsed.query)
            return
        if parsed.path == "/admin/diagnostics/export.zip":
            self._serve_diagnostics_export(parsed.query)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            path = urllib.parse.urlsplit(self.path).path
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid request")
            return
        if path == "/admin/playlists/portable/prepare":
            self._prepare_portable_playlist()
            return
        if path in {"/admin/sources/import/preview", "/admin/sources/import/apply"}:
            self._handle_source_import(path)
            return
        if path in {"/admin/sources/inspect", "/admin/sources/create"}:
            self._handle_source_post(path)
            return
        if path == "/admin/sources/audit":
            self._handle_source_audit()
            return
        if path == "/admin/sources/audits/export":
            self._handle_source_audit_export()
            return
        if path == "/admin/sources/audits/cancel":
            self._handle_audit_job_cancel()
            return
        if path in {
            "/admin/sources/update",
            "/admin/sources/set-enabled",
            "/admin/sources/public-release-mode",
        }:
            self._handle_source_mutation(path)
            return
        if path == "/admin/sources/delete":
            self._handle_source_delete()
            return
        super().do_POST()

    def _playlist_export_service(self) -> PlaylistExportService:
        if self.configuration_synchronization is None:
            raise PlaylistExportError("Playlist export is unavailable")
        store = (
            self.managed_admin_service.store
            if self.managed_admin_service
            else ManagedStateStore(self.data_dir / MANAGED_STATE_FILENAME)
        )
        return PlaylistExportService(
            store, self.configuration_synchronization, PortableCoverLoader(self.data_dir)
        )

    def _serve_portable_playlist(self, path: str, query: str) -> None:
        security = self._require_admin()
        if security is None:
            return
        try:
            if len(query) > 512 or urllib.parse.urlsplit(self.path).fragment:
                raise ValueError
            values = urllib.parse.parse_qs(
                query,
                strict_parsing=True,
                keep_blank_values=True,
                max_num_fields=2,
                errors="strict",
            )
            if set(values) != {"kind", "id"} or any(len(v) != 1 for v in values.values()):
                raise ValueError
            kind, object_id = values["kind"][0], values["id"][0]
            service = self._playlist_export_service()
            if path.endswith("export.zip"):
                identity, payload = service.export(kind, object_id)
                self._reply(
                    HTTPStatus.OK,
                    payload,
                    content_type="application/zip",
                    extra_headers={
                        "Content-Disposition": f'attachment; filename="playlist-{identity}-v1.zip"'
                    },
                )
            else:
                fingerprint, schema, existing_identity = service.preparation(kind, object_id)
                self._reply(
                    HTTPStatus.OK,
                    render_identity_confirmation(
                        kind,
                        object_id,
                        fingerprint,
                        schema,
                        existing_identity,
                        security.issue_csrf_token(),
                    ),
                )
        except PlaylistExportError as exc:
            self._source_error(HTTPStatus.CONFLICT, str(exc))
        except ManagedStateError, PortableSourceError, CustomCoverAssetError, OSError:
            self._source_error(
                HTTPStatus.CONFLICT, "Playlist configuration or selected cover is unavailable"
            )
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid playlist export selection")

    def _prepare_portable_playlist(self) -> None:
        security = self._require_admin()
        if security is None:
            return
        try:
            parsed = urllib.parse.urlsplit(self.path)
            lengths = self.headers.get_all("Content-Length", [])
            if (
                parsed.query
                or parsed.fragment
                or self.headers.get_all("Transfer-Encoding")
                or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,4}", lengths[0])
                or not 0 < int(lengths[0]) <= 4096
            ):
                raise ValueError
            form = self._read_form()
            token = single_form_value(form, "csrf_token")
            if not security.consume_csrf_token(token):
                self._source_error(HTTPStatus.FORBIDDEN, "Administration form validation failed")
                return
            if (
                set(form) - {"csrf_token", "kind", "id", "fingerprint", "confirm", "ack_v9"}
                or single_form_value(form, "confirm") != "yes"
            ):
                raise ValueError
            kind, object_id = single_form_value(form, "kind"), single_form_value(form, "id")
            ack = single_form_value(form, "ack_v9") if "ack_v9" in form else ""
            if ack not in {"", "yes"}:
                raise ValueError
            identity = self._playlist_export_service().prepare(
                kind, object_id, single_form_value(form, "fingerprint"), acknowledge_v9=ack == "yes"
            )
            self._reply(
                HTTPStatus.OK,
                render_identity_confirmation(
                    kind, object_id, "", 9, identity, security.issue_csrf_token()
                ),
            )
        except PlaylistExportError as exc:
            self._source_error(HTTPStatus.CONFLICT, str(exc))
        except ManagedStateError, OSError:
            self._source_error(HTTPStatus.CONFLICT, "Playlist identity preparation is unavailable")
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid identity confirmation")

    def _source_error(self, status: HTTPStatus, message: str) -> None:
        self._reply(status, message.encode("utf-8"), content_type="text/plain; charset=utf-8")

    def _source_import_service(self) -> SourceImportService:
        synchronization = self.configuration_synchronization
        if synchronization is None:
            raise SourceImportError("Source import is unavailable")
        service = self.managed_admin_service
        store = (
            service.store
            if service is not None
            else ManagedStateStore(self.data_dir / MANAGED_STATE_FILENAME)
        )
        provisioning = (
            service.provisioning_journal
            if service is not None
            else ProvisioningJournal(store.path.with_name(MANAGED_PROVISIONING_FILENAME))
        )
        return SourceImportService(store, synchronization, provisioning)

    def _handle_source_import(self, path: str) -> None:
        security = self._require_admin()
        if security is None:
            return
        if self.configuration_synchronization is None:
            self._source_error(HTTPStatus.SERVICE_UNAVAILABLE, "Source import is unavailable")
            return
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.query or parsed.fragment or self.headers.get_all("Transfer-Encoding"):
                raise ValueError("Invalid import request")
            lengths = self.headers.get_all("Content-Length", [])
            types = self.headers.get_all("Content-Type", [])
            if len(lengths) != 1 or len(types) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
                raise ValueError("Invalid import request headers")
            is_preview = path.endswith("/preview")
            limit = MAX_SOURCE_IMPORT_MULTIPART_BYTES if is_preview else 4096
            length = int(lengths[0])
            if not 0 < length <= limit:
                self.close_connection = True
                self._source_error(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Source import request is too large"
                )
                return
            service = self._source_import_service()
            if is_preview:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Incomplete source upload")
                token, document = parse_source_import_upload(types[0], body)
                if not security.consume_csrf_token(token):
                    self._source_error(
                        HTTPStatus.FORBIDDEN, "Administration form validation failed"
                    )
                    return
                preview = service.preview(document)
                staged = security.stage_source_import(document, preview.fingerprint)
                self._reply(
                    HTTPStatus.OK,
                    render_source_import_preview(
                        preview,
                        preview_id=staged.preview_id,
                        csrf_token=security.issue_csrf_token(),
                        lan_mode=self._is_lan_admin_mode(),
                    ),
                )
                return
            form = self._read_form()
            token = single_form_value(form, "csrf_token")
            if re.fullmatch(
                r"[A-Za-z0-9_-]{20,128}", token
            ) is None or not security.consume_csrf_token(token):
                self._source_error(HTTPStatus.FORBIDDEN, "Administration form validation failed")
                return
            # Only decisions are client supplied. The original bytes and fingerprint
            # live exclusively in this authenticated session's bounded context.
            preview_id = single_form_value(form, "preview_id")
            if re.fullmatch(r"[A-Za-z0-9_-]{20,128}", preview_id) is None:
                raise ValueError("Invalid preview reference")
            for name, values in form.items():
                if len(values) != 1 or (
                    name not in {"csrf_token", "preview_id", "action", "confirm", "ack_v8"}
                    and re.fullmatch(r"decision_(?:0|[1-9][0-9]?)", name) is None
                ):
                    raise ValueError("Invalid import confirmation fields")
            action = single_form_value(form, "action")
            if action not in {"apply", "cancel"}:
                raise ValueError("Invalid import action")
            context = security.take_source_import(preview_id)
            if context is None:
                self._source_error(
                    HTTPStatus.CONFLICT, "Preview expired or unavailable. Upload a new preview."
                )
                return
            decisions = {
                int(name.removeprefix("decision_")): values[0]
                for name, values in form.items()
                if name.startswith("decision_")
            }
            if any(value not in {"keep_local", "cancel"} for value in decisions.values()):
                raise ValueError("Invalid conflict decision")
            if action == "cancel" or "cancel" in decisions.values():
                self._reply(
                    HTTPStatus.OK,
                    render_source_import_result(None, lan_mode=self._is_lan_admin_mode()),
                )
                return
            if form.get("confirm") != ["1"] or form.get("ack_v8", ["0"]) not in (["0"], ["1"]):
                raise ValueError("Explicit import confirmation is required")
            result = service.apply(
                context.document,
                context.fingerprint,
                keep_local=frozenset(decisions),
                confirm_v8=form.get("ack_v8") == ["1"],
            )
            self._reply(
                HTTPStatus.OK,
                render_source_import_result(result, lan_mode=self._is_lan_admin_mode()),
            )
        except StaleSourceImport as exc:
            self._source_error(HTTPStatus.CONFLICT, str(exc))
        except SourceImportWriteError as exc:
            self._source_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        except UnsupportedSourceCapability:
            self._source_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "Source capability is unsupported. Nothing was imported.",
            )
        except PortableSourceError:
            self._source_error(
                HTTPStatus.BAD_REQUEST, "Invalid portable source JSON. Nothing was imported."
            )
        except SourceImportError as exc:
            self._source_error(HTTPStatus.CONFLICT, str(exc))
        except ManagedStateError, ProvisioningJournalError, OSError:
            self._source_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Source configuration is unavailable. No import was confirmed.",
            )
        except SourceImportCapacityError as exc:
            self._source_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source import request")

    def _serve_source_configuration(self, query: str) -> None:
        if self._require_admin() is None:
            return
        service = self.managed_admin_service
        synchronization = self.configuration_synchronization
        if synchronization is None:
            self._source_error(HTTPStatus.SERVICE_UNAVAILABLE, "Source export is unavailable")
            return
        try:
            if len(query) > 512:
                raise ValueError
            values = urllib.parse.parse_qs(
                query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=1,
                encoding="utf-8",
                errors="strict",
            )
            source_id = None
            if query:
                if set(values) != {"source_id"} or len(values["source_id"]) != 1:
                    raise ValueError
                source_id = values["source_id"][0]
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", source_id) is None:
                    raise ValueError
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source export selection")
            return
        try:
            payload = export_source_configuration(
                service.store
                if service is not None
                else ManagedStateStore(self.data_dir / MANAGED_STATE_FILENAME),
                synchronization,
                catalog=service.catalog if service is not None else BUILTIN_CATALOG,
                source_id=source_id,
            )
        except SourceMutationError:
            self._source_error(HTTPStatus.NOT_FOUND, "Configured source not found")
            return
        except NoSourcesToExportError:
            self._source_error(HTTPStatus.CONFLICT, "No sources are available to export")
            return
        except ManagedStateError, PortableSourceError, OSError:
            self._source_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Source export is unavailable")
            return
        filename = "sources-v1.json" if source_id is None else f"source-{source_id}-v1.json"
        self._reply(
            HTTPStatus.OK,
            payload,
            content_type="application/json",
            extra_headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _serve_source_new(self) -> None:
        security = self._require_admin()
        if security is None:
            return
        if self.source_admin_service is None:
            self._source_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._reply(
            HTTPStatus.OK,
            render_new_source_page(
                csrf_token=security.issue_csrf_token(), lan_mode=self._is_lan_admin_mode()
            ),
        )

    def _serve_source_edit(self, query: str) -> None:
        security = self._require_admin()
        if security is None:
            return
        service = self.source_admin_service
        if service is None:
            self._source_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        values = urllib.parse.parse_qs(query, keep_blank_values=True)
        if set(values) != {"source_id"} or len(values["source_id"]) != 1:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source ID")
            return
        try:
            source_id = values["source_id"][0]
            if source_id in {item.id for item in service.catalog.sources}:
                builtin = service.get_builtin_source(source_id)
                self._reply(
                    HTTPStatus.OK,
                    render_builtin_public_release_page(
                        builtin,
                        csrf_token=security.issue_csrf_token(),
                        lan_mode=self._is_lan_admin_mode(),
                    ),
                )
                return
            source = service.get_custom_source(source_id)
            state = service.snapshot()
        except SourceMutationError:
            self._source_error(HTTPStatus.NOT_FOUND, "Custom source not found")
            return
        except ManagedStateError, OSError:
            self._source_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Managed source state is unavailable"
            )
            return
        self._reply(
            HTTPStatus.OK,
            render_edit_source_page(
                source,
                used_by=tuple(
                    playlist.display_name
                    for playlist in state.playlists
                    if source.id in playlist.source_ids
                ),
                csrf_token=security.issue_csrf_token(),
                lan_mode=self._is_lan_admin_mode(),
            ),
        )

    def _serve_source_delete(self, query: str) -> None:
        security = self._require_admin()
        if security is None:
            return
        service = self.source_admin_service
        if service is None:
            self._source_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        values = urllib.parse.parse_qs(query, keep_blank_values=True)
        if set(values) != {"source_id"} or len(values["source_id"]) != 1:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source ID")
            return
        try:
            source, used_by, pending = service.deletion_references(values["source_id"][0])
        except SourceMutationError:
            self._source_error(HTTPStatus.NOT_FOUND, "Custom source not found")
            return
        except ManagedStateError, ProvisioningJournalError, OSError:
            self._source_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Source state is unavailable")
            return
        self._reply(
            HTTPStatus.OK,
            render_delete_source_page(
                source,
                used_by=used_by,
                pending=pending,
                csrf_token=security.issue_csrf_token(),
                lan_mode=self._is_lan_admin_mode(),
            ),
        )

    def _handle_source_delete(self) -> None:
        security = self._require_admin()
        if security is None:
            return
        service = self.source_admin_service
        if service is None:
            self._source_error(HTTPStatus.SERVICE_UNAVAILABLE, "Source deletion is unavailable")
            return
        try:
            form = self._read_form()
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source form")
            return
        tokens = form.get("csrf_token", [])
        if len(tokens) != 1 or not security.consume_csrf_token(tokens[0]):
            self._source_error(HTTPStatus.FORBIDDEN, "Administration form validation failed")
            return
        try:
            if set(form) != {"csrf_token", "source_id"}:
                raise ValueError
            source_id = single_form_value(form, "source_id")
            configured = service.delete(source_id)
        except EngineCycleBusyError:
            self._source_error(HTTPStatus.CONFLICT, "Engine cycle is active; retry shortly")
            return
        except SourceReferencedError:
            self._source_error(HTTPStatus.CONFLICT, "Unassign source before deletion")
            return
        except SourceMutationError:
            self._source_error(HTTPStatus.CONFLICT, "Custom source is unavailable")
            return
        except ManagedStateError, ProvisioningJournalError, PersistenceError, OSError, RuntimeError:
            self._source_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Source deletion is unavailable")
            return
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source form")
            return
        lifecycle = self.engine_lifecycle
        if lifecycle is not None:
            lifecycle.reconcile(configured=configured)
        self._admin_diagnostics().emit(
            occurred_at=datetime.now(UTC),
            severity=DiagnosticSeverity.INFO,
            component="admin",
            event_name="admin_source_deleted",
            source_id=source_id,
            details={
                "operation": "source_configuration",
                "outcome": "applied",
                "next_state": "deleted",
            },
        )
        self._reply(
            HTTPStatus.SEE_OTHER,
            b"",
            extra_headers={"Location": "/admin/sources?notice=source-deleted"},
        )

    def _handle_source_mutation(self, path: str) -> None:
        security = self._require_admin()
        if security is None:
            return
        service = self.source_admin_service
        if service is None:
            self._source_error(
                HTTPStatus.SERVICE_UNAVAILABLE, "Source administration is unavailable"
            )
            return
        try:
            form = self._read_form()
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source form")
            return
        tokens = form.get("csrf_token", [])
        if len(tokens) != 1 or not security.consume_csrf_token(tokens[0]):
            self._source_error(HTTPStatus.FORBIDDEN, "Administration form validation failed")
            return
        try:
            allowed = (
                {
                    "csrf_token",
                    "source_id",
                    "display_name",
                    "origin_kind",
                    "origin_code",
                    "language",
                    "editorial_scope",
                }
                if path == "/admin/sources/update"
                else {"csrf_token", "source_id", "public_release_mode"}
                if path == "/admin/sources/public-release-mode"
                else {"csrf_token", "source_id", "enabled"}
            )
            if path == "/admin/sources/update":
                allowed.update(
                    name for name in ("temporal_strategy", "public_release_mode") if name in form
                )
            if set(form) != allowed:
                raise ValueError("Invalid source form")
            values = {name: single_form_value(form, name) for name in allowed - {"csrf_token"}}
            if path == "/admin/sources/public-release-mode":
                builtin_updated, configured = service.update_builtin_public_release_mode(
                    values["source_id"], values["public_release_mode"]
                )
                updated_id = builtin_updated.id
                event_name, next_state, notice = "admin_source_updated", "updated", "source-updated"
            elif path == "/admin/sources/update":
                updated, configured = service.update_metadata(
                    values["source_id"],
                    display_name=values["display_name"],
                    origin_kind=values["origin_kind"],
                    origin_code=values["origin_code"],
                    language=values["language"],
                    editorial_scope=values["editorial_scope"],
                    temporal_strategy=values.get("temporal_strategy"),
                    public_release_mode=values.get("public_release_mode"),
                )
                event_name, next_state, notice = (
                    "admin_source_updated",
                    "updated",
                    "source-updated",
                )
            else:
                if values["enabled"] not in {"0", "1"}:
                    raise ValueError("Invalid source state")
                enabled = values["enabled"] == "1"
                updated, configured = service.set_enabled(values["source_id"], enabled)
                next_state = "enabled" if enabled else "disabled"
                event_name, notice = f"admin_source_{next_state}", f"source-{next_state}"
        except SourceDisableError as exc:
            self._source_error(HTTPStatus.CONFLICT, str(exc))
            return
        except SourceMutationError:
            self._source_error(HTTPStatus.CONFLICT, "Custom source is unavailable or not editable")
            return
        except ManagedStateError, OSError:
            self._source_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Managed source state is unavailable"
            )
            return
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source metadata or form")
            return
        if path != "/admin/sources/public-release-mode":
            updated_id = updated.id
        lifecycle = self.engine_lifecycle
        if lifecycle is not None:
            lifecycle.reconcile(configured=configured)
        self._admin_diagnostics().emit(
            occurred_at=datetime.now(UTC),
            severity=DiagnosticSeverity.INFO,
            component="admin",
            event_name=event_name,
            source_id=str(updated_id),
            details={
                "operation": "source_configuration",
                "outcome": "applied",
                "next_state": next_state,
            },
        )
        self._reply(
            HTTPStatus.SEE_OTHER,
            b"",
            extra_headers={"Location": f"/admin/sources?notice={notice}"},
        )

    def _serve_source_report(self) -> None:
        security = self._require_admin()
        if security is None:
            return
        context = security.source_inspection()
        if context is None:
            self._source_error(HTTPStatus.NOT_FOUND, "No active inspection report")
            return
        self._reply(
            HTTPStatus.OK,
            context.report_json,
            content_type="application/json; charset=utf-8",
            extra_headers={
                "Content-Disposition": (
                    f'attachment; filename="source-inspection-{context.spotify_show_id}.json"'
                )
            },
        )

    def _audit_job_parameters(self, query: str) -> str | None:
        values = urllib.parse.parse_qs(query, keep_blank_values=True)
        if set(values) != {"job"} or len(values["job"]) != 1:
            return None
        job_id = values["job"][0]
        if (
            not 12 <= len(job_id) <= 64
            or not job_id.isascii()
            or not all(character.isalnum() or character in "-_" for character in job_id)
        ):
            return None
        return job_id

    def _serve_audit_job(self, query: str, *, download: bool) -> None:
        security = self._require_admin()
        if security is None:
            return
        job_id = self._audit_job_parameters(query)
        jobs = self.source_audit_jobs
        job = jobs.get(job_id) if jobs is not None and job_id is not None else None
        if job is None:
            self._source_error(HTTPStatus.NOT_FOUND, "Audit job not found or expired")
            return
        if download:
            if job.status != "succeeded" or job.payload is None or job.filename is None:
                self._source_error(HTTPStatus.CONFLICT, "Audit ZIP is not ready")
                return
            self._reply(
                HTTPStatus.OK,
                job.payload,
                content_type="application/zip",
                extra_headers={"Content-Disposition": f'attachment; filename="{job.filename}"'},
            )
            return
        state = html.escape(job.status)
        finished = job.status != "running"
        details = (
            f'<p><a class="button button-primary" '
            f'href="/admin/sources/audits/download?job={job_id}">Download sanitized ZIP</a></p>'
            if job.status == "succeeded"
            else f"<p>{html.escape(job.error or 'Awaiting next source result')}</p>"
        )
        csrf = html.escape(security.issue_csrf_token(), quote=True)
        cancel = (
            f'<form method="post" action="/admin/sources/audits/cancel">'
            f'<input type="hidden" name="csrf_token" value="{csrf}">'
            f'<input type="hidden" name="job" value="{job_id}">'
            '<button class="button" type="submit">Cancel after current source</button></form>'
            if not finished
            else ""
        )
        body = (
            '<section class="section admin-section"><h2>Source audit export</h2>'
            f"<p>Status: <strong>{state}</strong>. Completed {job.completed} of "
            f"{job.total} sources. The rest of Admin remains available.</p>"
            + details
            + cancel
            + '<p><a href="/admin/sources">Back to sources</a></p></section>'
        )
        self._reply(
            HTTPStatus.OK,
            render_admin_shell(
                title="Source audit export",
                active_tab="sources",
                intro="Read-only background source inspection.",
                body_html=body,
            ),
            extra_headers={"Refresh": "3"} if not finished else None,
        )

    def _handle_audit_job_cancel(self) -> None:
        security = self._require_admin()
        if security is None:
            return
        try:
            form = self._read_form()
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid audit cancellation form")
            return
        tokens = form.get("csrf_token", [])
        if len(tokens) != 1 or not security.consume_csrf_token(tokens[0]):
            self._source_error(HTTPStatus.FORBIDDEN, "Administration form validation failed")
            return
        if set(form) != {"csrf_token", "job"} or len(form["job"]) != 1:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid audit cancellation form")
            return
        job_id = self._audit_job_parameters(urllib.parse.urlencode({"job": form["job"][0]}))
        jobs = self.source_audit_jobs
        if job_id is None or jobs is None or not jobs.cancel(job_id):
            self._source_error(HTTPStatus.NOT_FOUND, "Audit job not found")
            return
        self._reply(
            HTTPStatus.SEE_OTHER,
            b"",
            extra_headers={"Location": f"/admin/sources/audits/status?job={job_id}"},
        )

    def _handle_source_audit_export(self) -> None:
        security = self._require_admin()
        if security is None:
            return
        service = self.source_admin_service
        jobs = self.source_audit_jobs
        if service is None or jobs is None:
            self._source_error(HTTPStatus.SERVICE_UNAVAILABLE, "Source audit is unavailable")
            return
        try:
            form = self._read_form()
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source audit export form")
            return
        tokens = form.get("csrf_token", [])
        if len(tokens) != 1 or not security.consume_csrf_token(tokens[0]):
            self._source_error(HTTPStatus.FORBIDDEN, "Administration form validation failed")
            return
        if set(form) != {"csrf_token"}:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source audit export form")
            return
        diagnostics = self._admin_diagnostics()

        def on_event(event: str, completed: int, total: int) -> None:
            diagnostics.emit(
                occurred_at=datetime.now(UTC),
                severity=(
                    DiagnosticSeverity.WARNING
                    if event in {"failed", "cancelled"}
                    else DiagnosticSeverity.INFO
                ),
                component="admin",
                event_name=f"admin_source_audit_{event}",
                details={
                    "operation": "audit_export",
                    "completed_sources": completed,
                    "total_sources": total,
                },
            )

        try:
            job_id = jobs.start(service, build_revision=self.build_revision, on_event=on_event)
        except AuditJobBusyError:
            self._source_error(HTTPStatus.CONFLICT, "A source audit is already running")
            return
        self._reply(
            HTTPStatus.SEE_OTHER,
            b"",
            extra_headers={"Location": f"/admin/sources/audits/status?job={job_id}"},
        )

    def _handle_source_audit(self) -> None:
        security = self._require_admin()
        if security is None:
            return
        service = self.source_admin_service
        if service is None:
            self._source_error(HTTPStatus.SERVICE_UNAVAILABLE, "Source audit is unavailable")
            return
        try:
            form = self._read_form()
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source form")
            return
        tokens = form.get("csrf_token", [])
        if len(tokens) != 1 or not security.consume_csrf_token(tokens[0]):
            self._source_error(HTTPStatus.FORBIDDEN, "Administration form validation failed")
            return
        security.set_source_inspection(None)
        try:
            if set(form) != {"csrf_token", "source_id"}:
                raise ValueError
            report = service.audit(single_form_value(form, "source_id"))
            context = SourceInspectionContext(
                report.spotify_show_id,
                str(report.parser_id),
                report.collection_filter_id,
                serialize_source_inspection(report),
                purpose="configured",
            )
            payload = render_source_review_page(
                report,
                csrf_token=security.issue_csrf_token(),
                lan_mode=self._is_lan_admin_mode(),
                audit=True,
            )
            security.set_source_inspection(context)
            self._reply(HTTPStatus.OK, payload)
        except SourceMutationError:
            self._source_error(HTTPStatus.NOT_FOUND, "Configured source not found")
        except SourceInspectionError as exc:
            status = (
                HTTPStatus.INTERNAL_SERVER_ERROR
                if exc.failure is InspectionFailure.CONFIGURATION
                else HTTPStatus.BAD_GATEWAY
                if exc.failure is InspectionFailure.MALFORMED_RESPONSE
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self._source_error(status, str(exc))
        except ManagedStateError, OSError:
            self._source_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Source state is unavailable")
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source form")

    def _handle_source_post(self, path: str) -> None:
        security = self._require_admin()
        if security is None:
            return
        service = self.source_admin_service
        if service is None:
            self._source_error(
                HTTPStatus.SERVICE_UNAVAILABLE, "Source administration is unavailable"
            )
            return
        try:
            form = self._read_form()
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source form")
            return
        csrf_values = form.get("csrf_token", [])
        if len(csrf_values) != 1 or not security.consume_csrf_token(csrf_values[0]):
            self._source_error(HTTPStatus.FORBIDDEN, "Administration form validation failed")
            return
        if path == "/admin/sources/inspect":
            security.set_source_inspection(None)
        try:
            allowed = (
                {"csrf_token", "locator"}
                if path == "/admin/sources/inspect"
                else {
                    "csrf_token",
                    "spotify_show_id",
                    "display_name",
                    "origin_kind",
                    "origin_code",
                    "language",
                    "editorial_scope",
                    "timezone",
                    "parser_id",
                    "collection_filter_id",
                }
            )
            if path != "/admin/sources/inspect":
                allowed.update(
                    name for name in ("temporal_strategy", "public_release_mode") if name in form
                )
            if set(form) != allowed:
                raise ValueError("Invalid source form fields")
            if path == "/admin/sources/inspect":
                report = service.inspect(
                    single_form_value(form, "locator"),
                    parser_id=ParserId("release_date_title"),
                    collection_filter_id=None,
                )
                context = SourceInspectionContext(
                    report.spotify_show_id,
                    str(report.parser_id),
                    report.collection_filter_id,
                    serialize_source_inspection(report),
                )
                payload = render_source_review_page(
                    report,
                    csrf_token=security.issue_csrf_token(),
                    lan_mode=self._is_lan_admin_mode(),
                )
                security.set_source_inspection(context)
                self._reply(HTTPStatus.OK, payload)
                return

            values = {name: single_form_value(form, name) for name in allowed - {"csrf_token"}}
            reviewed = security.source_inspection()
            if reviewed is None or reviewed.purpose != "candidate":
                raise ValueError("Inspect this source before creating it")
            filter_id = values["collection_filter_id"] or None
            if (
                values["spotify_show_id"] != reviewed.spotify_show_id
                or values["parser_id"] != reviewed.parser_id
                or filter_id != reviewed.collection_filter_id
            ):
                raise ValueError("Source review no longer matches the inspected show")
            source = service.validate_new_source(
                show_id=values["spotify_show_id"],
                display_name=values["display_name"],
                origin_kind=values["origin_kind"],
                origin_code=values["origin_code"],
                language=values["language"],
                editorial_scope=values["editorial_scope"],
                timezone=values["timezone"],
                parser_id=values["parser_id"],
                collection_filter_id=filter_id,
                temporal_strategy=values.get("temporal_strategy", "auto"),
                public_release_mode=values.get("public_release_mode", "instant"),
            )
            service.create(source)
            security.set_source_inspection(None)
            self._reply(
                HTTPStatus.SEE_OTHER,
                b"",
                extra_headers={"Location": "/admin/sources?notice=source-added"},
            )
        except SourceIntakeError as exc:
            status = (
                HTTPStatus.CONFLICT
                if exc.failure is SourceIntakeFailure.DUPLICATE_SHOW
                else HTTPStatus.BAD_REQUEST
                if exc.failure
                in {
                    SourceIntakeFailure.INVALID_LOCATOR,
                    SourceIntakeFailure.STRATEGY,
                    SourceIntakeFailure.PROVIDER_REJECTION,
                }
                else HTTPStatus.UNPROCESSABLE_ENTITY
                if exc.failure
                in {
                    SourceIntakeFailure.INSUFFICIENT_COVERAGE,
                    SourceIntakeFailure.NO_USABLE_EPISODES,
                }
                else HTTPStatus.BAD_GATEWAY
                if exc.failure is SourceIntakeFailure.MALFORMED_RESPONSE
                else HTTPStatus.INTERNAL_SERVER_ERROR
                if exc.failure is SourceIntakeFailure.CONFIGURATION
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self._source_error(status, str(exc))
        except ManagedStateError, OSError:
            self._source_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Managed source state is unavailable"
            )
        except ValueError:
            self._source_error(HTTPStatus.BAD_REQUEST, "Invalid source classification or review")

    def _activate_managed_playlist(
        self,
        service: ManagedAdminService,
        form: Mapping[str, list[str]],
    ) -> str:
        auth = self.managed_admin_auth
        if auth is None:
            raise ManagedAdminError("Spotify authorization is required to create a playlist")
        access_token = auth.get_access_token()
        managed = service.activate(
            template_id=single_form_value(form, "template_id"),
            display_name=single_form_value(form, "display_name"),
            description=single_form_value(form, "description", required=False),
            cover_id=single_form_value(form, "cover_id"),
            source_ids=form.get("source_id", []),
            access_token=access_token,
            min_duration_minutes=duration_minutes_from_form(form, "min"),
            max_duration_minutes=duration_minutes_from_form(form, "max"),
        )
        return str(managed.id)

    def _update_managed_playlist(
        self,
        service: ManagedAdminService,
        form: Mapping[str, list[str]],
    ) -> tuple[str, bool]:
        playlist_id = playlist_id_from_form(form)
        name = single_form_value(form, "display_name")
        description = single_form_value(form, "description", required=False)
        cover_id = single_form_value(form, "cover_id")
        enabled_values = form.get("enabled", [])
        if enabled_values not in ([], ["1"]):
            raise ValueError("enabled must be omitted or set exactly once")
        enabled = bool(enabled_values)

        # Save is a local managed-state operation. Spotify metadata and cover are intentionally
        # synchronized only by the separate explicit action, so a disconnected/400 provider
        # cannot block source, duration, enable or desired-metadata changes.
        updated = service.update(
            playlist_id,
            display_name=name,
            description=description,
            cover_id=cover_id,
            source_ids=form.get("source_id", []),
            enabled=enabled,
            access_token=None,
            min_duration_minutes=duration_minutes_from_form(form, "min"),
            max_duration_minutes=duration_minutes_from_form(form, "max"),
        )
        return str(updated.id), updated.enabled

    def _serve_safe_public_status(self) -> None:
        ready = _data_dir_ready(self.data_dir)
        payload = _safe_operational_status_page(
            ready=ready,
            spotify_state=self._spotify_state(),
            status=self.operational_status,
            build_revision=self.build_revision,
        )
        self._reply(
            HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
            payload,
        )

    def _serve_sources(self, query: str = "") -> None:
        security = self._require_admin()
        if security is None:
            return
        service = self.managed_admin_service
        if service is None:
            self._reply(
                HTTPStatus.NOT_FOUND,
                b"Not found",
                content_type="text/plain; charset=utf-8",
            )
            return
        synchronization = self.configuration_synchronization
        try:
            if synchronization is None:
                snapshot = service.snapshot()
                state = service.store.load()
            else:
                with synchronization.hold():
                    snapshot = service.snapshot()
                    state = service.store.load()
            effective_sources(service.catalog, state)
        except ManagedStateError, OSError:
            self._reply(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                b"Managed playlist state could not be loaded safely",
                content_type="text/plain; charset=utf-8",
            )
            return
        status = self.operational_status
        last_cycle = None if status is None else status.snapshot().last_cycle
        notice_values = urllib.parse.parse_qs(query, keep_blank_values=True).get("notice", [])
        notice_key = notice_values[0] if len(notice_values) == 1 else ""
        self._reply(
            HTTPStatus.OK,
            render_sources_page(
                snapshot=snapshot,
                state=state,
                catalog=service.catalog,
                last_cycle=last_cycle,
                lan_mode=self._is_lan_admin_mode(),
                csrf_token=security.issue_csrf_token(),
                notice={
                    "source-added": "Custom source added successfully.",
                    "source-updated": "Custom source updated successfully.",
                    "source-enabled": "Custom source enabled successfully.",
                    "source-disabled": "Custom source disabled successfully.",
                    "source-deleted": "Custom source deleted successfully.",
                }.get(notice_key),
            ),
        )

    def _serve_diagnostics(self, query: str) -> None:
        if self._require_admin() is None:
            return
        try:
            filters, events, _ = self._diagnostic_events(query)
        except ValueError:
            self._invalid_diagnostic_filters()
            return
        except RuntimeError:
            self._diagnostic_history_unavailable()
            return
        self._reply(
            HTTPStatus.OK,
            render_diagnostics_page(
                events=events,
                filters=filters,
                display_timezone=self.diagnostic_timezone,
                timezone_label=self.diagnostic_timezone_label,
                spotify_metrics=self._spotify_metrics(),
            ),
        )

    def _serve_diagnostics_export(self, query: str) -> None:
        if self._require_admin() is None:
            return
        store = self.diagnostic_store
        if store is None:
            self._diagnostic_history_unavailable()
            return
        generated_at = datetime.now(UTC)
        try:
            filters, events, result_truncated = self._diagnostic_export_events(
                query,
                observed_at=generated_at,
            )
            retention_status = store.retention_status()
        except ValueError:
            self._invalid_diagnostic_filters()
            return
        except RuntimeError, PersistenceError:
            self._diagnostic_history_unavailable()
            return

        status = self.operational_status
        snapshot = None if status is None else status.snapshot()
        last_cycle = None if snapshot is None else snapshot.last_cycle
        payload = build_diagnostic_bundle(
            events=events,
            generated_at=generated_at,
            last_cycle=last_cycle,
            retention_days=store.retention_days,
            max_events=store.max_events,
            build_revision=self.build_revision,
            filters=filters,
            result_truncated=result_truncated,
            spotify_metrics=self._spotify_metrics(),
            retention_status=retention_status,
            export_limit=MAX_DIAGNOSTIC_EXPORT_EVENTS,
        )
        self._reply(
            HTTPStatus.OK,
            payload,
            content_type="application/zip",
            extra_headers={
                "Content-Disposition": 'attachment; filename="news-playlist-diagnostics.zip"'
            },
        )

    def _diagnostic_events(
        self,
        query: str,
    ) -> tuple[DiagnosticFilters, tuple[DiagnosticEvent, ...], bool | None]:
        store = self.diagnostic_store
        if store is None:
            raise RuntimeError("diagnostic store unavailable")
        filters = parse_diagnostic_filters(query)
        observed_at = datetime.now(UTC)
        since = filters.since(now=observed_at)
        try:
            events, result_truncated = store.list_events_with_truncation(
                since=since,
                severity=filters.severity,
                source_id=filters.source_id,
                playlist_id=filters.playlist_id,
                limit=filters.limit,
            )
        except PersistenceError as exc:
            raise RuntimeError("diagnostic store unavailable") from exc
        return filters, events, result_truncated

    def _diagnostic_export_events(
        self,
        query: str,
        *,
        observed_at: datetime,
    ) -> tuple[DiagnosticFilters, tuple[DiagnosticEvent, ...], bool]:
        store = self.diagnostic_store
        if store is None:
            raise RuntimeError("diagnostic store unavailable")
        filters = parse_diagnostic_filters(query)
        since = filters.since(now=observed_at)
        try:
            events, result_truncated = store.list_events_for_export_with_truncation(
                since=since,
                severity=filters.severity,
                source_id=filters.source_id,
                playlist_id=filters.playlist_id,
                limit=MAX_DIAGNOSTIC_EXPORT_EVENTS,
            )
        except PersistenceError as exc:
            raise RuntimeError("diagnostic store unavailable") from exc
        return filters, events, result_truncated

    def _spotify_metrics(self) -> SpotifyRequestMetricsSnapshot | None:
        metrics = self.spotify_request_metrics
        return None if metrics is None else metrics.last_24_hours()

    def _invalid_diagnostic_filters(self) -> None:
        self._reply(
            HTTPStatus.BAD_REQUEST,
            b"Invalid diagnostics filters",
            content_type="text/plain; charset=utf-8",
        )

    def _diagnostic_history_unavailable(self) -> None:
        self._reply(
            HTTPStatus.SERVICE_UNAVAILABLE,
            b"Diagnostic history is unavailable",
            content_type="text/plain; charset=utf-8",
        )


def serve(
    *,
    host: str = DEFAULT_HEALTH_HOST,
    port: int = DEFAULT_HEALTH_PORT,
    data_dir: Path = DEFAULT_DATA_DIR,
    stop_event: threading.Event | None = None,
    environ: Mapping[str, str] | None = None,
    interval: timedelta | None = None,
) -> int:
    """Run the production engine using the diagnostics-aware HTTP handler."""
    handler = DiagnosticOperationalHealthHandler
    previous_timezone = handler.diagnostic_timezone
    previous_timezone_label = handler.diagnostic_timezone_label
    previous_build_revision = handler.build_revision
    display_timezone, timezone_label = _diagnostic_display_timezone(environ)
    handler.diagnostic_timezone = display_timezone
    handler.diagnostic_timezone_label = timezone_label
    handler.build_revision = resolve_build_revision(environ)
    try:
        return engine_runtime.serve(
            host=host,
            port=port,
            data_dir=data_dir,
            stop_event=stop_event,
            environ=environ,
            interval=interval,
            handler_type=handler,
        )
    finally:
        handler.diagnostic_timezone = previous_timezone
        handler.diagnostic_timezone_label = previous_timezone_label
        handler.build_revision = previous_build_revision


def _diagnostic_display_timezone(
    environ: Mapping[str, str] | None,
) -> tuple[tzinfo, str]:
    env = os.environ if environ is None else environ
    timezone_name = env.get("TZ", "").strip()
    if not timezone_name:
        return UTC, "UTC"
    try:
        return ZoneInfo(timezone_name), timezone_name
    except ZoneInfoNotFoundError, ValueError:
        return UTC, "UTC"


def _safe_operational_status_page(
    *,
    ready: bool,
    spotify_state: AuthorizationState | None,
    status: OperationalStatus | None,
    build_revision: str = "dev",
) -> bytes:
    snapshot = (
        OperationalStatus(configured=False).snapshot() if status is None else status.snapshot()
    )
    runtime_label = "Ready" if ready else "Degraded"
    storage_label = "Writable" if ready else "Unavailable"
    engine_label = "Not configured"
    engine_tone: BadgeTone = "muted"
    if snapshot.configured:
        engine_label = "Cycle running" if snapshot.running else "Scheduled"
        engine_tone = "accent" if snapshot.running else "success"

    cycle = snapshot.last_cycle
    cycle_result = "Not run yet" if cycle is None else ("Success" if cycle.ok else "Failed")
    cycle_tone: BadgeTone = "muted" if cycle is None else ("success" if cycle.ok else "danger")
    cycle_started = "—" if cycle is None else _format_time(cycle.started_at)
    cycle_finished = "—" if cycle is None else _format_time(cycle.finished_at)
    next_run = "—" if snapshot.next_run_at is None else _format_time(snapshot.next_run_at)
    source_rows = _safe_source_rows(cycle)
    playlist_rows = _safe_playlist_rows(cycle)
    spotify_label = authorization_state_label(spotify_state)
    spotify_tone: BadgeTone = (
        "success" if spotify_state is AuthorizationState.CONNECTED else "muted"
    )
    runtime_tone: BadgeTone = "success" if ready else "danger"
    overall_label = "Operational"
    overall_tone: BadgeTone = "success"
    if not ready:
        overall_label = "Runtime degraded"
        overall_tone = "danger"
    elif cycle is not None and not cycle.ok:
        overall_label = "Cycle needs attention"
        overall_tone = "warning"
    elif snapshot.running:
        overall_label = "Cycle running"
        overall_tone = "accent"

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>News Bulletin Playlists</title>
  <style>{WEB_UI_STYLES}</style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <a class="brand" href="/" aria-label="News Bulletin Playlists status">
        <span class="brand-mark" aria-hidden="true">NB</span>
        <span>News Bulletin Playlists</span>
      </a>
      {status_badge(overall_label, tone=overall_tone)}
    </header>

    <main id="main-content">
      <section class="hero" aria-labelledby="page-title">
        <div>
          <p class="eyebrow">Live operational status</p>
          <h1 id="page-title">Your news playlists, always current.</h1>
          <p class="hero-copy">
            A read-only view of the engine, Spotify connection, sources and published playlists.
            Detailed failure context stays inside authenticated administration.
          </p>
          <p class="hero-copy">
            Pre-release: built-in playlist templates and custom sources are supported;
            creating arbitrary new playlist templates is not available yet.
          </p>
        </div>
        <div class="hero-actions">
          <a class="button button-primary" href="/admin/">Administration</a>
          <span class="section-note">Configuration and diagnostics are protected.</span>
        </div>
      </section>

      <section aria-label="Runtime summary">
        <div class="summary-grid">
          <article class="card summary-card">
            <p class="card-label">Runtime</p>
            <p class="card-value">{status_badge(runtime_label, tone=runtime_tone)}</p>
            <p class="card-meta">Persistent storage: {html.escape(storage_label)}</p>
          </article>
          <article class="card summary-card">
            <p class="card-label">Spotify</p>
            <p class="card-value">{status_badge(spotify_label, tone=spotify_tone)}</p>
            <p class="card-meta">Authorization state</p>
          </article>
          <article class="card summary-card">
            <p class="card-label">Engine</p>
            <p class="card-value">{status_badge(engine_label, tone=engine_tone)}</p>
            <p class="card-meta">Next run: {next_run}</p>
          </article>
          <article class="card summary-card">
            <p class="card-label">Last cycle</p>
            <p class="card-value">{status_badge(cycle_result, tone=cycle_tone)}</p>
            <p class="card-meta">Started {cycle_started} · ended {cycle_finished}</p>
          </article>
        </div>
      </section>

      <section class="section" aria-labelledby="sources-heading">
        <div class="section-head">
          <div>
            <p class="eyebrow">Collection</p>
            <h2 id="sources-heading">Sources</h2>
          </div>
          <p class="section-note">Public-safe source health and catalogue freshness.</p>
        </div>
        <div class="card table-card">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Source</th>
                  <th scope="col">Result</th>
                  <th scope="col">Detector success</th>
                  <th scope="col">Catalogue refresh</th>
                  <th scope="col">Spotify state</th>
                </tr>
              </thead>
              <tbody>{source_rows}</tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="section" aria-labelledby="playlists-heading">
        <div class="section-head">
          <div>
            <p class="eyebrow">Publishing</p>
            <h2 id="playlists-heading">Playlists</h2>
          </div>
          <p class="section-note">Desired, verified and write state from the latest cycle.</p>
        </div>
        <div class="card table-card">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Playlist</th>
                  <th scope="col">Result</th>
                  <th scope="col">Last success</th>
                  <th scope="col">Items</th>
                  <th scope="col">Write</th>
                </tr>
              </thead>
              <tbody>{playlist_rows}</tbody>
            </table>
          </div>
        </div>
      </section>
    </main>

    <footer class="footer">
      <p>Read-only status · sensitive diagnostics are not exposed here.</p>
      <p>Copyright (c) 2026 Roberto Ribes</p>
      <p>Version <code>{html.escape(__version__)}</code> · Build
        <code>{html.escape(build_revision)}</code></p>
    </footer>
  </div>
</body>
</html>
"""
    return document.encode("utf-8")


def _safe_source_rows(cycle: EngineCycleResult | None) -> str:
    if cycle is None or not cycle.sources:
        return '<tr><td colspan="5">No source cycle data yet.</td></tr>'
    rows = []
    for source in cycle.sources:
        result = (
            status_badge("Success", tone="success")
            if source.ok
            else status_badge("Failed — see admin diagnostics", tone="danger")
        )
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(source.source_id))}</code></td>"
            f"<td>{result}</td>"
            f"<td>{_format_optional_time(source.last_detector_success_at)}</td>"
            f"<td>{_format_optional_time(source.last_catalogue_refresh_at)}</td>"
            f"<td>{source.episode_count} cached / {source.api_request_count} API requests</td>"
            "</tr>"
        )
    return "".join(rows)


def _safe_playlist_rows(cycle: EngineCycleResult | None) -> str:
    if cycle is None or not cycle.playlists:
        return '<tr><td colspan="5">No playlist cycle data yet.</td></tr>'
    rows = []
    for playlist in cycle.playlists:
        result = (
            status_badge("Success", tone="success")
            if playlist.ok
            else status_badge("Failed — see admin diagnostics", tone="danger")
        )
        applied = "unverified" if playlist.applied_count is None else str(playlist.applied_count)
        write = "—" if playlist.wrote is None else ("updated" if playlist.wrote else "unchanged")
        write_tone: BadgeTone = (
            "muted" if playlist.wrote is None else ("accent" if playlist.wrote else "success")
        )
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(playlist.playlist_id))}</code></td>"
            f"<td>{result}</td>"
            f"<td>{_format_optional_time(playlist.last_success_at)}</td>"
            f"<td>{playlist.desired_count} desired / {applied} verified</td>"
            f"<td>{status_badge(write, tone=write_tone)}</td>"
            "</tr>"
        )
    return "".join(rows)


def _format_optional_time(value: datetime | None) -> str:
    return "—" if value is None else _format_time(value)


def _format_time(value: datetime) -> str:
    timestamp = value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return html.escape(timestamp)
