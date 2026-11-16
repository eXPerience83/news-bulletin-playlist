"""First-boot operator report uses inert original data, never OAuth or Spotify."""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from pathlib import Path

import pytest
from _legacy_dataset import legacy_root

from news_bulletin_playlist.cli import main
from news_bulletin_playlist.inspection_fence import startup_gate
from news_bulletin_playlist.upgrade_inspection_report import (
    UpgradeInspectionError,
    upgrade_inspection_report,
)
from news_bulletin_playlist.v11_operational_storage import V11OperationalStore


@pytest.fixture(autouse=True)
def reject_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Inspection report tried to access an external network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_first_boot_reports_four_exact_managed_destinations_and_frozen_policies(
    tmp_path: Path, schema: int
):
    root, preview = legacy_root(tmp_path, schema)
    legacy_bytes = (root / "managed-state.json").read_bytes()
    startup_gate(root, requested=True)
    owner = V11OperationalStore(root)
    installed = owner.initialize_from_legacy()
    state_bytes = owner.load_bytes()
    report = upgrade_inspection_report(root)

    assert report["format"] == "news-bulletin-playlist.v11-inspection-report"
    assert report["gate"] == "held"
    assert report["legacy_file_present"] is True
    assert report["legacy_comparison"] == "matching"
    assert report["safe_to_review_without_spotify_writes"] is True
    assert report["ready_for_production"] is False
    assert all(report["checks"].values())
    assert len(report["playlists"]) == 4
    assert [p["spotify_playlist_id"] for p in report["playlists"]] == [
        p.destination.external_id for p in preview.original_runtime.playlists
    ]
    assert [p["source_ids"] for p in report["playlists"]] == [
        list(p.source_ids) for p in installed.managed_overlay.playlists
    ]
    assert [s["id"] for s in report["sources"]] == [s.id for s in preview.original_runtime.sources]
    assert [s["parser_id"] for s in report["sources"]] == [
        s.parser_id for s in preview.original_runtime.sources
    ]
    assert len(report["historical_covers"]) == len(
        {template.cover_id for template in installed.owner_inventory.templates.records}
    )
    assert all(c["persisted_and_verified"] for c in report["historical_covers"])
    assert owner.load_bytes() == state_bytes
    assert (root / "managed-state.json").read_bytes() == legacy_bytes


def test_changed_legacy_bytes_fail_closed_even_when_v11_is_valid(tmp_path: Path):
    root, _ = legacy_root(tmp_path, 10)
    startup_gate(root, requested=True)
    owner = V11OperationalStore(root)
    owner.initialize_from_legacy()
    unchanged = owner.load_bytes()
    (root / "managed-state.json").write_bytes(b'{"schema_version":10}')
    with pytest.raises(UpgradeInspectionError, match="changed"):
        upgrade_inspection_report(root)
    assert owner.load_bytes() == unchanged


def test_missing_inert_legacy_file_cannot_be_misrepresented_as_clean_install(
    tmp_path: Path,
):
    root, _ = legacy_root(tmp_path, 10)
    startup_gate(root, requested=True)
    owner = V11OperationalStore(root)
    owner.initialize_from_legacy()
    before = owner.load_bytes()
    (root / "managed-state.json").unlink()
    with pytest.raises(UpgradeInspectionError, match="missing"):
        upgrade_inspection_report(root)
    assert owner.load_bytes() == before


def test_inspection_report_marks_operator_changed_managed_destinations(tmp_path: Path):
    root, _ = legacy_root(tmp_path, 10)
    startup_gate(root, requested=True)
    owner = V11OperationalStore(root)
    previous = owner.initialize_from_legacy()
    before = owner.load_bytes()
    edited = replace(
        previous.managed_overlay,
        playlists=tuple(replace(p, enabled=False) for p in previous.managed_overlay.playlists),
    )
    owner.save(edited, expected_bytes=before)
    report = upgrade_inspection_report(root)
    assert report["legacy_comparison"] == "changed"
    assert not report["checks"]["managed_destinations_unchanged"]
    assert report["safe_to_review_without_spotify_writes"] is False


def test_unheld_report_never_treats_liveness_as_remote_write_authorization(tmp_path: Path):
    root, _ = legacy_root(tmp_path, 6)
    V11OperationalStore(root).initialize_from_legacy()
    report = upgrade_inspection_report(root)
    assert report["gate"] == "not_requested"
    assert not report["safe_to_review_without_spotify_writes"]
    assert report["checks"]["inspection_hold_active"] is False


def test_clean_install_report_has_zero_preset_destinations(tmp_path: Path):
    root = tmp_path / "fresh"
    root.mkdir()
    V11OperationalStore(root).initialize_fresh()
    startup_gate(root, requested=True)
    report = upgrade_inspection_report(root)
    assert report["legacy_file_present"] is False
    assert report["legacy_comparison"] == "not_applicable"
    assert report["sources"] == []
    assert report["playlists"] == []
    assert report["owner_templates"] == []
    assert report["safe_to_review_without_spotify_writes"] is True
    assert report["ready_for_production"] is False


def test_cli_prints_same_machine_readable_inspection_and_no_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    root, _ = legacy_root(tmp_path, 6)
    startup_gate(root, requested=True)
    owner = V11OperationalStore(root)
    owner.initialize_from_legacy()
    before = owner.load_bytes()
    assert main(["inspection-report", "--data-dir", str(root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["gate"] == "held"
    assert payload["v11_state_sha256"] == upgrade_inspection_report(root)["v11_state_sha256"]
    assert len(payload["playlists"]) == 4
    assert owner.load_bytes() == before
