"""Pre-cutover inventory has no side effects and matches held v11 semantics."""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import pytest
from _legacy_dataset import legacy_root

from news_bulletin_playlist.cli import main
from news_bulletin_playlist.inspection_fence import startup_gate
from news_bulletin_playlist.legacy_preflight import LegacyPreflightError, legacy_preflight
from news_bulletin_playlist.upgrade_inspection_report import upgrade_inspection_report
from news_bulletin_playlist.v11_operational_storage import V11OperationalStore


@pytest.fixture(autouse=True)
def reject_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        pytest.fail("legacy-preflight attempted provider I/O")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_read_only_original_inventory_equals_held_v11_report(tmp_path: Path, schema: int) -> None:
    root, _preview = legacy_root(tmp_path, schema)
    originals = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    report = legacy_preflight(root, expected_destinations=4)
    assert report["read_only"] is True
    assert report["ready_for_production"] is False
    assert report["legacy_schema_version"] == schema
    assert report["destination_count_matches_expectation"] is True
    assert report["destination_count"] == 4
    assert report["source_count"] >= 13
    assert report["template_count"] >= 7
    assert (
        report["legacy_state_sha256"] == hashlib.sha256(originals["managed-state.json"]).hexdigest()
    )
    assert originals == {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}

    startup_gate(root, requested=True)
    V11OperationalStore(root).initialize_from_legacy()
    held = upgrade_inspection_report(root)
    assert held["gate"] == "held"
    assert held["legacy_comparison"] == "matching"
    assert report["legacy_state_sha256"] == held["historical_origin_sha256"]
    assert report["sources"] == held["sources"]
    assert report["playlists"] == held["playlists"]
    assert report["owner_templates"] == held["owner_templates"]
    assert report["historical_cover_ids"] == [row["id"] for row in held["historical_covers"]]
    with pytest.raises(LegacyPreflightError, match="v11 authority"):
        legacy_preflight(root)


def test_expected_count_mismatch_is_reported_and_cli_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = legacy_root(tmp_path, 10)
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    exit_code = main(["legacy-preflight", "--data-dir", str(root), "--expect-destinations", "5"])
    assert exit_code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["destination_count"] == 4
    assert result["destination_count_matches_expectation"] is False
    assert before == {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


@pytest.mark.parametrize("bad", ["missing", "symlink", "invalid", "duplicate", "witness"])
def test_invalid_legacy_evidence_fails_without_creating_v11(tmp_path: Path, bad: str) -> None:
    root, _ = legacy_root(tmp_path, 10)
    file = root / "managed-state.json"
    if bad == "missing":
        file.unlink()
    elif bad == "symlink":
        file.unlink()
        file.symlink_to(root / "oauth-private")
    elif bad == "invalid":
        file.write_bytes(b"{")
    elif bad == "duplicate":
        file.write_bytes(b'{"schema_version":10,"schema_version":10}')
    else:
        (root / ".managed-state-v11.authority").write_bytes(b"witness")
    with pytest.raises(LegacyPreflightError):
        legacy_preflight(root, expected_destinations=4)
    assert not (root / "managed-state-v11.json").exists()


def test_preflight_rejects_original_changed_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import news_bulletin_playlist.legacy_preflight as module

    root, _ = legacy_root(tmp_path, 6)
    original_read = module._read_regular
    calls = 0

    def inconsistent(directory: int, filename: str, limit: int) -> bytes:
        nonlocal calls
        data = original_read(directory, filename, limit)
        calls += 1
        return data if calls == 1 else data + b" "

    monkeypatch.setattr(module, "_read_regular", inconsistent)
    with pytest.raises(LegacyPreflightError, match="changed"):
        legacy_preflight(root)
