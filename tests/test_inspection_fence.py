"""Offline release-gated first-boot cutover; no Spotify HTTP calls or external provider I/O."""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest
from _admin_http import login as _login
from _admin_http import request as _request
from _legacy_dataset import legacy_root as _legacy_root
from _source_admin_http_fixtures import _server

from news_bulletin_playlist import engine_runtime
from news_bulletin_playlist.cli import main
from news_bulletin_playlist.engine_runtime import EngineLifecycleController
from news_bulletin_playlist.inspection_fence import (
    INSPECTION_ENV,
    INSPECTION_FILENAME,
    RELEASE_CONFIRMATION,
    InspectionGateError,
    inspect_gate,
    inspection_requested,
    release_gate,
    startup_gate,
)
from news_bulletin_playlist.v11_operational_storage import V11OperationalStore


@pytest.fixture(autouse=True)
def forbid_real_spotify(monkeypatch: pytest.MonkeyPatch) -> None:
    original_connect = socket.socket.connect

    def local_only(sock: socket.socket, address: object) -> object:
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            pytest.fail("Inspection test attempted provider network I/O")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_a: pytest.fail("network"))


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_hold_is_durable_and_precedes_forward_only_conversion(tmp_path: Path, schema: int):
    root, original = _legacy_root(tmp_path, schema)
    legacy_bytes = (root / "managed-state.json").read_bytes()
    assert startup_gate(root, requested=True) is True
    gate_path = root / INSPECTION_FILENAME
    assert gate_path.is_file() and gate_path.stat().st_mode & 0o077 == 0
    assert not (root / "managed-state-v11.json").exists()
    converted = V11OperationalStore(root).initialize_from_legacy()
    assert converted.runtime == original.original_runtime
    assert (root / "managed-state.json").read_bytes() == legacy_bytes
    snapshot = inspect_gate(root)
    assert snapshot.held and len(snapshot.state_sha256) == 64
    assert startup_gate(root, requested=False)
    assert startup_gate(root, requested=False)
    assert inspect_gate(root) == snapshot


def test_release_requires_operator_confirmation_and_exact_inspected_bytes(tmp_path: Path):
    root, _ = _legacy_root(tmp_path, 10)
    startup_gate(root, requested=True)
    V11OperationalStore(root).initialize_from_legacy()
    status = inspect_gate(root)
    path = root / INSPECTION_FILENAME
    held_bytes = path.read_bytes()

    for wrong_hash, confirm in (
        ("0" * 64, RELEASE_CONFIRMATION),
        (status.state_sha256, "wrong"),
        ("not-hex", RELEASE_CONFIRMATION),
    ):
        with pytest.raises(InspectionGateError):
            release_gate(root, expected_sha256=wrong_hash, confirm=confirm)
        assert path.read_bytes() == held_bytes

    released = release_gate(
        root, expected_sha256=status.state_sha256, confirm=RELEASE_CONFIRMATION
    )
    assert released.state == "released"
    assert not startup_gate(root, requested=False)
    assert inspect_gate(root).original_release_sha256 == status.state_sha256
    with pytest.raises(InspectionGateError, match="No held"):
        release_gate(root, expected_sha256=status.state_sha256, confirm=RELEASE_CONFIRMATION)
    assert startup_gate(root, requested=True)
    assert inspect_gate(root).held
    assert not startup_gate(root, requested=False) is False


def test_changed_v11_state_invalidates_operator_release(tmp_path: Path):
    root, _ = _legacy_root(tmp_path, 10)
    startup_gate(root, requested=True)
    owner = V11OperationalStore(root)
    owner.initialize_from_legacy()
    previous = inspect_gate(root).state_sha256
    managed = owner.load().managed_overlay
    from dataclasses import replace

    owner.save(replace(managed, playlists=tuple(
        replace(item, enabled=False) for item in managed.playlists
    )), expected_bytes=owner.load_bytes())
    assert inspect_gate(root).state_sha256 != previous
    with pytest.raises(InspectionGateError, match="changed"):
        release_gate(root, expected_sha256=previous, confirm=RELEASE_CONFIRMATION)
    assert inspect_gate(root).held


@pytest.mark.parametrize("value", ["true", "yes", "2", "-1", "", " 1 ", "False"])
def test_invalid_inspection_mode_is_not_silently_ignored(value: str):
    with pytest.raises(InspectionGateError, match=INSPECTION_ENV):
        inspection_requested({INSPECTION_ENV: value})


@pytest.mark.parametrize("tamper", ["symlink", "public", "bad_json", "noncanonical", "directory"])
def test_corrupt_inspection_marker_blocks_normal_start_and_release(
    tmp_path: Path, tamper: str
):
    root = tmp_path / "data"
    root.mkdir()
    startup_gate(root, requested=True)
    marker = root / INSPECTION_FILENAME
    if tamper == "symlink":
        marker.unlink()
        marker.symlink_to(root / "elsewhere")
    elif tamper == "public":
        os.chmod(marker, 0o644)
    elif tamper == "bad_json":
        marker.write_bytes(b"{")
    elif tamper == "noncanonical":
        marker.write_bytes(marker.read_bytes().replace(b'"version":1', b'"version":1, '))
    else:
        marker.unlink()
        marker.mkdir()
    with pytest.raises(InspectionGateError):
        startup_gate(root, requested=False)
    with pytest.raises(InspectionGateError):
        startup_gate(root, requested=True)


class _NeverRunCycle:
    calls = 0

    def run_cycle(self) -> object:
        self.calls += 1
        pytest.fail("Inspection gate allowed engine cycle")


def test_held_lifecycle_reconcile_wake_restart_and_shutdown_never_start_scheduler():
    runner = _NeverRunCycle()
    controller = EngineLifecycleController(runner, interval=engine_runtime.timedelta(seconds=1))
    controller.hold_for_inspection()
    for value in (True, True, False, True):
        controller.reconcile(configured=value)
        controller.wake()
        assert controller.scheduler is None
        assert controller.status.snapshot().next_run_at is None
        assert not controller.status.snapshot().running
        assert not controller.status.snapshot().configured
    assert controller.shutdown()
    assert runner.calls == 0


def test_inspection_http_status_and_all_admin_posts_are_blocked(tmp_path: Path):
    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        server.RequestHandlerClass.inspection_hold = True
        try:
            status, _, html = _request(server, "GET", "/", cookie=cookie)
            assert status == 200
            assert b"INSPECTION HOLD" in html or b"Inspection hold" in html
            status, _, payload = _request(server, "GET", "/healthz")
            assert status == 200
            assert json.loads(payload)["spotify_reconciliation"] == "inspection_held"
            status, _, page = _request(server, "GET", "/admin/", cookie=cookie)
            assert status == 200
            assert b"Startup inspection hold" in page
            for target in (
                "/admin/spotify/connect",
                "/admin/playlists/activate",
                "/admin/playlists/sync",
                "/admin/sources/inspect",
                "/admin/sources/audit",
                "/admin/sources/create",
                "/admin/sources/import/apply",
                "/admin/playlists/import/apply",
                "/admin/playlists/portable/prepare",
                "/admin/owner-covers/upload",
                "/admin/provisioning/adopt",
            ):
                result = _request(
                    server, "POST", target, cookie=cookie, form={"csrf_token": "bad"}
                )
                assert result[0] == 423, target
            result = _request(
                server, "GET", "/admin/spotify/callback?code=private&state=private", cookie=cookie
            )
            assert result[0] == 423
            assert client.calls == []
            assert store.load_readonly().playlists == ()
        finally:
            server.RequestHandlerClass.inspection_hold = False


class _FakeAuth:
    def __init__(self) -> None:
        self.calls = 0

    def set_request_metrics(self, _metrics: object) -> None:
        return None

    def get_access_token(self, *_a: object, **_kw: object) -> str:
        self.calls += 1
        pytest.fail("Inspection startup tried to access Spotify")


@pytest.mark.parametrize("schema", [6, 10])
def test_serve_direct_cutover_and_next_restart_do_not_run_enabled_playlists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: int
):
    root, original = _legacy_root(tmp_path, schema)
    auth = _FakeAuth()

    monkeypatch.setattr(
        engine_runtime, "build_engine_runtime_auth", lambda *_a, **_kw: (None, auth)
    )
    # Failure here would mean an automatic first playlist write was possible.
    monkeypatch.setattr(
        engine_runtime.EngineLifecycleController, "_start_scheduler_locked",
        lambda _self: pytest.fail("inspection startup scheduled an engine cycle"),
    )
    monkeypatch.setattr(
        engine_runtime.ManagedAdminService, "finalize_known_provisioning",
        lambda _self: pytest.fail("inspection startup finalized pending Spotify creation"),
    )

    class Handler(engine_runtime.OperationalHealthHandler):
        pass

    stop = threading.Event()
    stop.set()
    assert engine_runtime.serve(
        host="127.0.0.1", port=0, data_dir=root, stop_event=stop,
        environ={INSPECTION_ENV: "1"}, handler_type=Handler
    ) == 0
    assert V11OperationalStore(root).load().runtime == original.original_runtime
    assert inspect_gate(root).held
    assert Handler.inspection_hold is False
    assert auth.calls == 0

    # The flag can disappear entirely; an unreviewed next boot remains PAUSED.
    assert engine_runtime.serve(
        host="127.0.0.1", port=0, data_dir=root, stop_event=stop,
        environ={}, handler_type=Handler
    ) == 0
    assert inspect_gate(root).held
    assert auth.calls == 0


def test_cli_status_is_read_only_and_release_is_explicit(tmp_path: Path, capsys):
    root, _ = _legacy_root(tmp_path, 10)
    startup_gate(root, requested=True)
    V11OperationalStore(root).initialize_from_legacy()
    before = (root / "managed-state-v11.json").read_bytes()
    assert main(["inspection-status", "--data-dir", str(root)]) == 0
    assert inspect_gate(root).state_sha256 in capsys.readouterr().out
    assert (root / "managed-state-v11.json").read_bytes() == before
    assert main([
        "inspection-release", "--data-dir", str(root),
        "--state-sha256", inspect_gate(root).state_sha256,
        "--confirm", RELEASE_CONFIRMATION,
    ]) == 0
    assert "normal restart" in capsys.readouterr().out
    assert not inspect_gate(root).held
    assert (root / "managed-state-v11.json").read_bytes() == before
