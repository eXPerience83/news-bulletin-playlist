from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".github/workflows/ci.yml"


def _read() -> str:
    return CI_PATH.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str | None) -> str:
    _, separator, remainder = text.partition(start)
    assert separator, f"missing workflow anchor: {start!r}"
    if end is None:
        return remainder
    block, separator, _ = remainder.partition(end)
    assert separator, f"missing workflow anchor: {end!r}"
    return block


def _active(block: str) -> str:
    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))


def test_pull_request_ci_paths_cover_tested_contract_inputs() -> None:
    trigger = _active(_between(_read(), "on:\n", "\npermissions:"))
    push = _between(trigger, "  push:\n", "  pull_request:\n")
    pull_request = _between(trigger, "  pull_request:\n", None)

    assert "branches: [main]" in push
    assert "paths:" not in push

    required = (
        '      - "src/**"',
        '      - "tests/**"',
        '      - "scripts/**/*.py"',
        '      - "pyproject.toml"',
        '      - ".github/workflows/ci.yml"',
        '      - ".github/workflows/publish-pr-candidate-amd64.yml"',
        '      - ".github/workflows/publish-pr-candidate-worker-amd64.yml"',
    )
    for path in required:
        assert path in pull_request, f"missing CI path dependency: {path}"

    unrelated = (
        '      - "README.md"',
        '      - "docs/**"',
        '      - "Dockerfile"',
        '      - ".dockerignore"',
        '      - "compose.yaml"',
        '      - "deploy/**"',
        '      - "assets/covers/**"',
    )
    for path in unrelated:
        assert path not in pull_request, f"unrelated path should not allocate Python CI: {path}"
