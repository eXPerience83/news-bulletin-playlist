"""Imported JSON is bounded, ephemeral and isolated between authenticated sessions."""

from datetime import UTC, datetime

import pytest

from news_bulletin_playlist.admin_session import (
    ADMIN_SESSION_IDLE_TTL,
    MAX_SOURCE_IMPORT_PREVIEWS,
    SOURCE_IMPORT_PREVIEW_TTL,
    AdminSessionManager,
)

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def test_context_is_document_bound_session_bound_and_consumed_once():
    manager = AdminSessionManager()
    first = manager.create_session(now=NOW)
    second = manager.create_session(now=NOW)
    context = manager.stage_source_import(first, b"original bytes", "configuration", now=NOW)
    assert manager.take_source_import(second, context.preview_id, now=NOW) is None
    assert manager.take_source_import(first, "wrong-id", now=NOW) is None
    assert manager.take_source_import(first, context.preview_id, now=NOW) == context
    assert context.document == b"original bytes" and context.fingerprint == "configuration"
    assert manager.take_source_import(first, context.preview_id, now=NOW) is None


def test_new_upload_replaces_old_context():
    manager = AdminSessionManager()
    session = manager.create_session(now=NOW)
    first = manager.stage_source_import(session, b"first", "config", now=NOW)
    second = manager.stage_source_import(session, b"second", "config", now=NOW)
    assert first.preview_id != second.preview_id
    assert manager.take_source_import(session, first.preview_id, now=NOW) is None
    assert manager.take_source_import(session, second.preview_id, now=NOW) == second


@pytest.mark.parametrize("ending", ["preview_expiry", "idle_expiry", "logout"])
def test_expiry_and_logout_clear_preview(ending):
    manager = AdminSessionManager()
    session = manager.create_session(now=NOW)
    context = manager.stage_source_import(session, b"document", "config", now=NOW)
    observed = NOW
    if ending == "preview_expiry":
        observed += SOURCE_IMPORT_PREVIEW_TTL
    elif ending == "idle_expiry":
        observed += ADMIN_SESSION_IDLE_TTL
    else:
        manager.invalidate_session(session)
    assert manager.take_source_import(session, context.preview_id, now=observed) is None


def test_global_preview_cap_and_expiry_reclaim_memory():
    manager = AdminSessionManager()
    sessions = [manager.create_session(now=NOW) for _ in range(MAX_SOURCE_IMPORT_PREVIEWS + 1)]
    for session in sessions[:-1]:
        manager.stage_source_import(session, b"doc", "config", now=NOW)
    with pytest.raises(ValueError, match="Too many"):
        manager.stage_source_import(sessions[-1], b"doc", "config", now=NOW)
    # Replacing one's own preview needs no extra slot.
    manager.stage_source_import(sessions[0], b"updated", "config", now=NOW)
    context = manager.stage_source_import(
        sessions[-1], b"doc", "config", now=NOW + SOURCE_IMPORT_PREVIEW_TTL
    )
    assert (
        manager.take_source_import(
            sessions[-1], context.preview_id, now=NOW + SOURCE_IMPORT_PREVIEW_TTL
        )
        == context
    )


@pytest.mark.parametrize("document", [b"", b"x" * (256 * 1024 + 1), "not bytes"])
def test_staging_checks_document_byte_bound(document):
    manager = AdminSessionManager()
    session = manager.create_session(now=NOW)
    with pytest.raises(ValueError, match="size"):
        manager.stage_source_import(session, document, "config", now=NOW)
