from __future__ import annotations

import io
import json
import logging
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics import DiagnosticEventStore, DiagnosticSeverity
from news_bulletin_playlist.diagnostics_web import build_diagnostic_bundle
from news_bulletin_playlist.managed_state import ManagedState
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.source_inspection import (
    InspectionFailure,
    InspectionResponseReason,
    SourceInspectionError,
    inspect_configured_source,
)
from news_bulletin_playlist.spotify.catalogue_errors import (
    CatalogueItemValueKind,
    CatalogueValidationReason,
)
from news_bulletin_playlist.spotify.first_catalogue import (
    CatalogueResponseError,
    fetch_source_catalogue,
)
from news_bulletin_playlist.spotify.public_detector import spotify_show_id
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
SOURCE = BUILTIN_CATALOG.sources[0]
URL = "https://secret-external-url.test/secret-token"
SECRETS = (
    "secret-token",
    "secret-refresh-token",
    "Authorization",
    "secret-provider-body",
    "secret-title",
    "secret-episode-id",
    URL,
    "spotify:episode:",
    "secret-exception-string",
    "secret-rejected-value",
    "secret-provider-string",
    "secret-provider-array-entry",
    "secret-uri",
    "SecretNonJsonType",
)


def _item(position: int) -> dict[str, Any]:
    episode_id = f"{position:022d}"
    return {
        "id": episode_id,
        "uri": f"spotify:episode:{episode_id}",
        "name": "secret-title",
        "release_date": "2026-10-07",
        "release_date_precision": "day",
        "duration_ms": 60_000,
        "access_token": "secret-token",
        "refresh_token": "secret-refresh-token",
        "Authorization": "Bearer secret-token",
        "raw_body": "secret-provider-body",
        "external_urls": {"spotify": URL},
    }


class SecretNonJsonType:
    def __repr__(self) -> str:
        raise AssertionError("rejected item repr must never be read")


NON_OBJECT_CASES = {
    "item_empty_array": ([], CatalogueItemValueKind.ARRAY),
    "item_array": (["secret-provider-array-entry", _item(96)], CatalogueItemValueKind.ARRAY),
    "item_string": ("secret-provider-string", CatalogueItemValueKind.STRING),
    "item_integer": (123, CatalogueItemValueKind.NUMBER),
    "item_float": (123.5, CatalogueItemValueKind.NUMBER),
    "item_true": (True, CatalogueItemValueKind.BOOLEAN),
    "item_false": (False, CatalogueItemValueKind.BOOLEAN),
    "item_object": (object(), CatalogueItemValueKind.OTHER),
    "item_hostile_repr": (SecretNonJsonType(), CatalogueItemValueKind.OTHER),
}


# All existing guards, including both sides of each important disjunction.
CASES = [
    ("page_none", "page_not_object"),
    ("page_string", "page_not_object"),
    ("items_missing", "items_not_list"),
    ("items_string", "items_not_list"),
    ("limit_bool", "invalid_limit"),
    ("limit_value", "invalid_limit"),
    ("offset_bool", "invalid_offset"),
    ("offset_value", "invalid_offset"),
    ("total_bool", "invalid_total"),
    ("total_type", "invalid_total"),
    ("total_negative", "invalid_total"),
    ("total_increase", "total_changed"),
    ("total_decrease", "total_changed"),
    ("next_type", "invalid_next"),
    ("next_blank", "invalid_next"),
    ("next_extra", "next_contradiction"),
    ("next_absent", "next_contradiction"),
    ("paging_count", "impossible_paging"),
    ("paging_total", "impossible_paging"),
    *((case, "item_not_object") for case in NON_OBJECT_CASES),
    ("id_missing", "invalid_episode_id"),
    ("id_type", "invalid_episode_id"),
    ("id_format", "invalid_episode_id"),
    ("uri_type", "invalid_episode_uri"),
    ("uri_format", "invalid_episode_uri"),
    ("uri_mismatch", "episode_uri_id_mismatch"),
    ("title_blank", "invalid_title"),
    ("title_type", "invalid_title"),
    ("date_type", "invalid_release_date"),
    ("date_calendar", "invalid_release_date"),
    ("date_format", "invalid_release_date"),
    ("precision_type", "invalid_release_precision"),
    ("precision_hour", "invalid_release_precision"),
    ("duration_negative", "invalid_duration"),
    ("duration_bool", "invalid_duration"),
    ("duration_type", "invalid_duration"),
    ("duplicate", "duplicate_episode_id"),
    ("duplicate_same_page", "duplicate_episode_id"),
]


def _pages(case: str) -> list[Any]:
    if case in NON_OBJECT_CASES:
        first = catalogue_page([_item(i) for i in range(50)], total=97, next_url=URL)
        second = catalogue_page([_item(i) for i in range(50, 97)], offset=50, total=97)
        second["items"][46] = NON_OBJECT_CASES[case][0]
        return [first, second]
    total = 53 if case == "next_absent" else 52
    first = catalogue_page([_item(i) for i in range(50)], total=total, next_url=URL)
    second = catalogue_page([_item(50), _item(51)], offset=50, total=total)
    page_changes = {
        "items_missing": ("items", None),
        "items_string": ("items", "secret-rejected-value"),
        "limit_bool": ("limit", True),
        "limit_value": ("limit", "secret-rejected-value"),
        "offset_bool": ("offset", False),
        "offset_value": ("offset", 0),
        "total_bool": ("total", True),
        "total_type": ("total", "secret-rejected-value"),
        "total_negative": ("total", -1),
        "total_increase": ("total", 53),
        "total_decrease": ("total", 51),
        "next_type": ("next", {"secret": URL}),
        "next_blank": ("next", " "),
        "next_extra": ("next", URL),
    }
    item_changes = {
        "id_type": ("id", None),
        "id_format": ("id", "secret-episode-id"),
        "uri_type": ("uri", None),
        "uri_format": ("uri", URL),
        "uri_mismatch": ("uri", f"spotify:episode:{'Z' * 22}"),
        "title_blank": ("name", " "),
        "title_type": ("name", {"secret": "secret-title"}),
        "date_type": ("release_date", None),
        "date_calendar": ("release_date", "2026-02-30"),
        "date_format": ("release_date", "secret-rejected-value"),
        "precision_type": ("release_date_precision", None),
        "precision_hour": ("release_date_precision", "hour"),
        "duration_negative": ("duration_ms", -1),
        "duration_bool": ("duration_ms", True),
        "duration_type": ("duration_ms", "secret-rejected-value"),
    }
    if case in page_changes:
        key, value = page_changes[case]
        second[key] = value
    elif case in item_changes:
        key, value = item_changes[case]
        second["items"][1][key] = value
    elif case == "page_none":
        return [first, None]
    elif case == "page_string":
        return [first, "secret-provider-body"]
    elif case == "paging_count":
        first["total"] = second["total"] = 101
        second["items"] = [_item(i) for i in range(50, 101)]
    elif case == "paging_total":
        second["items"].append(_item(52))
    elif case == "id_missing":
        del second["items"][1]["id"]
    elif case == "duplicate":
        second["items"][1] = _item(0)
    elif case == "duplicate_same_page":
        second["items"][1] = _item(50)
    else:
        assert case == "next_absent"
    return [first, second]


class _Client:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, int]] = []

    def show_details(self, show_id: str) -> dict[str, Any]:
        self.calls.append(("details", 0))
        return {"id": show_id, "name": "secret-title", "raw_body": "secret-provider-body"}

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> Any:
        assert show_id == spotify_show_id(SOURCE)
        assert limit == 50
        self.calls.append(("episodes", offset))
        return self.pages[offset // 50]


class _Auth:
    def get_access_token(self, *, now: datetime | None = None) -> str:
        return "secret-token"


def _safe(serialized: str) -> None:
    for secret in (*SECRETS, *(f"{i:022d}" for i in range(97)), "Z" * 22):
        assert secret not in serialized


@pytest.mark.parametrize(("case", "reason"), CASES)
def test_catalogue_error_and_persisted_exported_diagnostic(
    tmp_path: Path,
    case: str,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _Client(_pages(case))
    with pytest.raises(CatalogueResponseError) as raised:
        fetch_source_catalogue(client, SOURCE, now=NOW, cutoff=NOW - timedelta(days=2))
    error = raised.value
    assert error.reason is CatalogueValidationReason(reason)
    assert error.requested_offset == 50
    item_reasons = {
        "item_not_object",
        "invalid_episode_id",
        "invalid_episode_uri",
        "episode_uri_id_mismatch",
        "invalid_title",
        "invalid_release_date",
        "invalid_release_precision",
        "invalid_duration",
        "duplicate_episode_id",
    }
    item_index = 46 if case in NON_OBJECT_CASES else 1 if reason in item_reasons else None
    assert error.item_index == item_index
    kind = NON_OBJECT_CASES[case][1] if case in NON_OBJECT_CASES else None
    assert error.item_value_kind is kind
    assert client.calls == [("episodes", 0), ("episodes", 50)]
    _safe(str(error) + repr(vars(error)))

    # A free-form message/extra attribute must never enter the diagnostic mapping.
    client = _Client(_pages(case))
    journal = SpotifyRateLimitJournal(SQLiteStore(tmp_path / "state.sqlite3"))
    diagnostics = journal.diagnostics
    assert diagnostics is not None and diagnostics.store is not None
    store = diagnostics.store
    diagnostics.logger = logging.getLogger("catalogue-test")
    original_init = CatalogueResponseError.__init__

    def with_arbitrary_message(self: CatalogueResponseError, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.args = ("secret-exception-string",)
        self.raw_payload = "secret-provider-body"

    monkeypatch.setattr(CatalogueResponseError, "__init__", with_arbitrary_message)
    with pytest.raises(SourceInspectionError) as public:
        inspect_configured_source(
            SOURCE.id,
            catalog=BUILTIN_CATALOG,
            state=ManagedState(),
            auth=_Auth(),
            rate_limit=journal,
            now=NOW,
            client_factory=lambda token: client,
            observe_public=False,
            clock=lambda: NOW,
        )
    assert public.value.failure is InspectionFailure.MALFORMED_RESPONSE
    assert public.value.response_reason is InspectionResponseReason.CATALOGUE_SHAPE
    _safe(str(public.value) + repr(vars(public.value)))
    assert client.calls == [("details", 0), ("episodes", 0), ("episodes", 50)]
    events, truncated = store.list_events_for_export_with_truncation(limit=100)
    assert not truncated and len(events) == 1
    event = events[0]
    assert event.event_name == "source_inspection_catalogue_shape_invalid"
    expected: dict[str, Any] = {
        "failure_class": "response_shape_error",
        "phase": "collection",
        "catalogue_reason": reason,
        "requested_offset": 50,
    }
    if item_index is not None:
        expected["item_index"] = item_index
    if kind is not None:
        expected["item_value_kind"] = kind.value
    if reason == "total_changed":
        expected.update(expected_total=52, observed_total=53 if case == "total_increase" else 51)
    assert event.details == expected
    _safe(json.dumps(event.details))
    _safe(caplog.text)
    if kind is not None:
        assert f"item_value_kind={kind.value}" in caplog.text
    with sqlite3.connect(store.path) as connection:
        persisted = connection.execute("SELECT details_json FROM diagnostic_events").fetchone()[0]
        assert json.loads(persisted) == expected
        _safe(persisted)
    payload = build_diagnostic_bundle(
        events=events,
        generated_at=NOW,
        last_cycle=None,
        retention_days=30,
        max_events=1000,
        result_truncated=truncated,
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        documents = "\n".join(archive.read(name).decode() for name in archive.namelist())
        assert json.loads(archive.read("diagnostics.jsonl"))["details"] == expected
        _safe(documents)
        if kind is not None:
            assert f"item_value_kind={kind.value}" in archive.read("diagnostics.txt").decode()


def test_matrix_covers_closed_reason_set() -> None:
    assert {reason for _, reason in CASES} == {reason.value for reason in CatalogueValidationReason}


@pytest.mark.parametrize("duration", [None, 0, "absent"])
def test_valid_optional_duration_unicode_and_extra_fields(duration: object) -> None:
    item = _item(0)
    item.update(name="Últimas noticias — 世界", restricted={"reason": "market"}, is_playable=False)
    if duration == "absent":
        del item["duration_ms"]
    else:
        item["duration_ms"] = duration
    snapshot = fetch_source_catalogue(
        _Client([catalogue_page([item])]),
        SOURCE,
        now=NOW,
        cutoff=NOW - timedelta(days=2),
        collect_evidence=True,
    )
    assert snapshot.complete and snapshot.coverage_proven and snapshot.request_count == 1
    assert snapshot.api_latest_id == item["id"]
    assert len(snapshot.episodes) == 1
    assert snapshot.episodes[0].title == item["name"]
    assert snapshot.episodes[0].duration_seconds == (0 if duration == 0 else None)


@pytest.mark.parametrize("old_boundary", [False, True])
def test_bounded_200_prefix_contract(old_boundary: bool) -> None:
    pages = []
    for offset in range(0, 200, 50):
        items = [_item(i) for i in range(offset, offset + 50)]
        if old_boundary:
            for item in items:
                item["release_date"] = "2026-10-01"
        pages.append(catalogue_page(items, offset=offset, total=250, next_url=URL))
    client = _Client(pages)
    snapshot = fetch_source_catalogue(client, SOURCE, now=NOW, cutoff=NOW - timedelta(days=2))
    assert len(snapshot.api_ids) == len(snapshot.episodes) == 200
    assert snapshot.request_count == 4
    assert not snapshot.complete
    assert snapshot.coverage_proven is old_boundary
    assert snapshot.api_latest_id is None
    assert client.calls == [("episodes", offset) for offset in (0, 50, 100, 150)]


@pytest.mark.parametrize(
    "key",
    ["catalogue_reason", "requested_offset", "item_index", "expected_total", "observed_total"],
)
@pytest.mark.parametrize(
    "value", [True, None, -1, 1.5, "secret-rejected-value", {"raw": "secret-provider-body"}]
)
def test_new_diagnostic_fields_reject_noncontract_values(
    tmp_path: Path, key: str, value: Any
) -> None:
    store = DiagnosticEventStore(tmp_path / "events.sqlite3")
    store.initialize()
    with pytest.raises(ValueError):
        store.record(
            occurred_at=NOW,
            severity=DiagnosticSeverity.WARNING,
            component="source_inspection",
            event_name="source_inspection_catalogue_shape_invalid",
            details={key: value},
        )
    assert not store.list_events()


@pytest.mark.parametrize("with_reason", [False, True])
def test_old_catalogue_events_remain_exportable(tmp_path: Path, with_reason: bool) -> None:
    store = DiagnosticEventStore(tmp_path / "events.sqlite3")
    store.initialize()
    details = {"failure_class": "response_shape_error", "phase": "collection"}
    if with_reason:
        details.update(catalogue_reason="item_not_object", requested_offset=50, item_index=46)
    store.record(
        occurred_at=NOW,
        severity=DiagnosticSeverity.WARNING,
        component="source_inspection",
        event_name="source_inspection_catalogue_shape_invalid",
        details=details,
    )
    events = store.list_events()
    assert events[0].details == details
    payload = build_diagnostic_bundle(
        events=events, generated_at=NOW, last_cycle=None, retention_days=30, max_events=1000
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert json.loads(archive.read("diagnostics.jsonl"))["details"] == details


@pytest.mark.parametrize("failure_type", [PersistenceError, ValueError])
def test_diagnostic_persistence_failure_preserves_public_catalogue_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
) -> None:
    journal = SpotifyRateLimitJournal(SQLiteStore(tmp_path / "state.sqlite3"))
    assert journal.diagnostics is not None and journal.diagnostics.store is not None

    def fail(**kwargs: Any) -> int:
        raise failure_type("secret-exception-string")

    monkeypatch.setattr(journal.diagnostics.store, "record", fail)
    client = _Client(_pages("duration_bool"))
    with pytest.raises(SourceInspectionError) as public:
        inspect_configured_source(
            SOURCE.id,
            catalog=BUILTIN_CATALOG,
            state=ManagedState(),
            auth=_Auth(),
            rate_limit=journal,
            now=NOW,
            client_factory=lambda token: client,
            observe_public=False,
            clock=lambda: NOW,
        )
    assert public.value.failure is InspectionFailure.MALFORMED_RESPONSE
    assert public.value.response_reason is InspectionResponseReason.CATALOGUE_SHAPE
    _safe(str(public.value))
    assert client.calls == [("details", 0), ("episodes", 0), ("episodes", 50)]


@pytest.mark.parametrize("value", ["dict", "secret-provider-value", "", None, 123, True])
def test_item_value_kind_rejects_noncontract_diagnostic_values(tmp_path: Path, value: Any) -> None:
    store = DiagnosticEventStore(tmp_path / "events.sqlite3")
    store.initialize()
    with pytest.raises(ValueError):
        store.record(
            occurred_at=NOW,
            severity=DiagnosticSeverity.WARNING,
            component="source_inspection",
            event_name="source_inspection_catalogue_shape_invalid",
            details={"item_value_kind": value},
        )
    assert not store.list_events()


@pytest.mark.parametrize(
    ("reason", "index", "kind"),
    [
        (CatalogueValidationReason.ITEM_NOT_OBJECT, 46, None),
        (CatalogueValidationReason.ITEM_NOT_OBJECT, None, CatalogueItemValueKind.NULL),
        (CatalogueValidationReason.ITEM_NOT_OBJECT, 46, "null"),
        (CatalogueValidationReason.INVALID_DURATION, 46, CatalogueItemValueKind.NULL),
    ],
)
def test_item_value_kind_exception_invariant(
    reason: CatalogueValidationReason, index: int | None, kind: Any
) -> None:
    with pytest.raises(ValueError):
        CatalogueResponseError(reason, requested_offset=50, item_index=index, item_value_kind=kind)


def test_closed_item_value_kinds_are_all_covered() -> None:
    assert {kind for _, kind in NON_OBJECT_CASES.values()} == set(CatalogueItemValueKind) - {
        CatalogueItemValueKind.NULL
    }
    assert {kind.value for kind in CatalogueItemValueKind} == {
        "null",
        "array",
        "string",
        "number",
        "boolean",
        "other",
    }
