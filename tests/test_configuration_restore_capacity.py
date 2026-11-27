"""Exercise actual near-limit configuration ZIPs and report isolated Linux RSS.

The fixture stresses repeated large playlist artwork, not every possible archive
composition. Measurements are evidence for #667, not a production memory budget.
Run with pytest -s tests/test_configuration_restore_capacity.py.
"""

import io
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from random import Random
from uuid import UUID

import pytest
from _file_snapshots import snapshot_files as _files
from _playlist_template_fixtures import _draft
from PIL import Image
from test_configuration_restore import _donor, _receiver

from news_bulletin_playlist.configuration_export import export_configuration
from news_bulletin_playlist.configuration_restore import (
    MAX_RESTORE_BYTES,
    ConfigurationRestoreError,
    ConfigurationRestoreService,
)
from news_bulletin_playlist.custom_cover_assets import MAX_JPEG_BYTES, CustomCoverAssetStore
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_provisioning import (
    MANAGED_PROVISIONING_FILENAME,
    ProvisioningJournal,
)
from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.portable_playlists import PortableCoverLoader, playlist_bundle
from news_bulletin_playlist.v11_managed_adapter import ForwardOnlyManagedStateStore


def _forbid_network(*args, **kwargs):
    raise AssertionError("Capacity rehearsal attempted network I/O")


def _large_jpeg():
    pixels = Random(667).randbytes(512 * 512 * 3)
    with Image.frombytes("RGB", (512, 512), pixels) as image:
        for quality in (90, 85, 80, 75, 70):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality)
            jpeg = output.getvalue()
            if 128 * 1024 <= len(jpeg) <= MAX_JPEG_BYTES:
                return jpeg
    raise AssertionError("Could not build a large bounded synthetic JPEG")


def _drafts(count, cover_id):
    return tuple(
        _draft(
            id=PlaylistId(f"custom_capacity_{index:03d}"),
            portable_id=str(UUID(int=index + 1, version=4)),
            cover_id=cover_id,
        )
        for index in range(count)
    )


def _measure(document_path, workdir, expected_drafts):
    # Linux ru_maxrss can retain the parent's high-water mark across exec.
    # VmHWM belongs to the current memory map, so fixture generation in the
    # parent does not mask the peak of this freshly exec'd interpreter.
    socket.socket.connect = _forbid_network

    def report(stage):
        status = Path("/proc/self/status").read_text(encoding="ascii")
        fields = next(line.split() for line in status.splitlines() if line.startswith("VmHWM:"))
        assert fields[2] == "kB"
        peak = int(fields[1])
        print(f"configuration capacity: {stage} peak_rss_kib={peak}", flush=True)

    report("baseline")
    document = document_path.read_bytes()
    print(f"configuration capacity: archive_bytes={len(document)}", flush=True)
    root, service = _receiver(workdir)
    before = _files(root)
    preview = service.preview(document)
    report("preview")
    assert preview.original_drafts == expected_drafts
    assert preview.recovered_managed == 4
    assert before == _files(root)
    service.apply(document, preview.fingerprint, confirmed=True)
    report("apply_cumulative")
    del service
    restored_files = _files(root)
    restarted = ConfigurationRestoreService(
        ForwardOnlyManagedStateStore(root),
        ConfigurationSynchronization(),
        ProvisioningJournal(root / MANAGED_PROVISIONING_FILENAME),
        CustomCoverAssetStore(root / "custom-covers"),
    )
    state = restarted.store.load_readonly()
    assert not state.playlists
    assert len(state.templates) == expected_drafts + 4
    repeat = restarted.preview(document)
    assert repeat.already_restored
    assert restarted.apply(document, repeat.fingerprint, confirmed=True).preview.already_restored
    assert restored_files == _files(root)
    report("restart_and_repeat_cumulative")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux RSS units are required")
def test_configuration_restore_capacity(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(socket.socket, "connect", _forbid_network)
    baseline = _donor(tmp_path)
    root = tmp_path / "legacy"
    store = ForwardOnlyManagedStateStore(root)
    state = store.load_readonly()
    cover_id = CustomCoverAssetStore(root / "custom-covers").install_canonical(_large_jpeg())
    one = replace(state, templates=_drafts(1, cover_id))
    store.save(one)
    _, member = playlist_bundle(
        one, store, "draft", one.templates[0].id, PortableCoverLoader(root), version=3
    )
    # Allow headroom for per-member outer ZIP headers/manifest and donor
    # differences; assert the resulting real archive size rather than assume it.
    count = (MAX_RESTORE_BYTES - len(baseline) - 512 * 1024) // (len(member) + 1024)
    assert 0 < count < 248
    _, sizing_service = _receiver(tmp_path)
    store.save(replace(state, templates=_drafts(count, cover_id)))
    document = export_configuration(root, sizing_service.synchronization)
    assert MAX_RESTORE_BYTES * 9 // 10 <= len(document) <= MAX_RESTORE_BYTES
    document_path = tmp_path / "near-limit.zip"
    document_path.write_bytes(document)

    workdir = tmp_path / "measurement"
    workdir.mkdir()
    result = subprocess.run(
        [sys.executable, __file__, str(document_path), str(workdir), str(count)],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with capsys.disabled():
        print(result.stdout, end="")

    # A genuine export over the restore limit must be refused by both entry
    # points; do not manufacture a malformed padded ZIP or lower the limit.
    store.save(replace(state, templates=_drafts(count + 8, cover_id)))
    oversized = export_configuration(root, sizing_service.synchronization)
    assert len(oversized) > MAX_RESTORE_BYTES
    before = _files(tmp_path / "receiver")
    with pytest.raises(ConfigurationRestoreError, match="size limit"):
        sizing_service.preview(oversized)
    with pytest.raises(ConfigurationRestoreError, match="size limit"):
        sizing_service.apply(oversized, "0" * 64, confirmed=True)
    assert before == _files(tmp_path / "receiver")


if __name__ == "__main__":
    _measure(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]))
