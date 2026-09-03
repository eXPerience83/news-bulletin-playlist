from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    ruff = shutil.which("ruff")
    if ruff is None:
        raise SystemExit(
            "ruff was not found; activate the development environment and install .[dev] first"
        )

    commands = (
        (ruff, "check", "--fix", "."),
        (ruff, "format", "."),
    )
    for command in commands:
        completed = subprocess.run(command, cwd=repo_root, check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
