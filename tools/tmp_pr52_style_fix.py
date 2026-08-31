from pathlib import Path

path = Path("tests/test_managed_admin_runtime.py")
text = path.read_text()
old = 'def __init__(self, synchronization: "_TrackingSynchronization") -> None:'
new = 'def __init__(self, synchronization: _TrackingSynchronization) -> None:'
if text.count(old) != 1:
    raise SystemExit("expected quoted tracking annotation exactly once")
path.write_text(text.replace(old, new))
