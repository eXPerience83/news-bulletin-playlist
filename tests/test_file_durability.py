from __future__ import annotations

import os
from pathlib import Path

import pytest

from news_bulletin_playlist import file_durability


def test_fsync_directory_uses_strongest_supported_directory_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []

    def fake_open(path: Path, flags: int) -> int:
        calls.append(("open", (path, flags)))
        return 41

    def fake_fsync(descriptor: int) -> None:
        calls.append(("fsync", descriptor))

    def fake_close(descriptor: int) -> None:
        calls.append(("close", descriptor))

    monkeypatch.setattr(file_durability.os, "open", fake_open)
    monkeypatch.setattr(file_durability.os, "fsync", fake_fsync)
    monkeypatch.setattr(file_durability.os, "close", fake_close)

    file_durability.fsync_directory(tmp_path)

    assert calls == [
        ("open", (tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))),
        ("fsync", 41),
        ("close", 41),
    ]


def test_fsync_directory_closes_descriptor_and_preserves_oserror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    closed: list[int] = []

    def fake_open(path: Path, flags: int) -> int:
        del path, flags
        return 43

    def fail_fsync(descriptor: int) -> None:
        del descriptor
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(file_durability.os, "open", fake_open)

    monkeypatch.setattr(file_durability.os, "fsync", fail_fsync)
    monkeypatch.setattr(file_durability.os, "close", closed.append)

    with pytest.raises(OSError, match="simulated directory fsync failure"):
        file_durability.fsync_directory(tmp_path)

    assert closed == [43]
