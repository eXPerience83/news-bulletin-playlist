"""Exercise the packaged cutover shell gate using a deterministic Docker double.

No Docker daemon, published image, real provider, OAuth, or external network.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify-v11-container-cutover.sh"

_FAKE_DOCKER = """#!/bin/sh
set -eu
root="$FAKE_DOCKER_DIR"
printf '%s\\n' "$*" >> "$root/events"
case "$1 $2" in
  'volume create')
    touch "$root/volume"
    printf '%s\\n' "$3"
    ;;
  'volume rm')
    rm -f "$root/volume"
    ;;
  'run --rm')
    case " $* " in
      *' --network none '*) : ;;
      *) echo 'Offline seed requested external network' >&2; exit 9 ;;
    esac
    ;;
  'run -d')
    case " $* " in
      *' --network none '*) : ;;
      *) echo 'Offline runtime requested external network' >&2; exit 9 ;;
    esac
    count="$(cat "$root/starts")"
    printf '%s\\n' "$((count + 1))" > "$root/starts"
    touch "$root/container"
    echo synthetic-container
    ;;
  'inspect --format')
    count="$(cat "$root/inspections")"
    printf '%s\\n' "$((count + 1))" > "$root/inspections"
    if [ "$FAKE_DOCKER_SCENARIO" = timeout ]; then
      echo starting
    else
      echo healthy
    fi
    ;;
  'exec '* )
    if [ "$(cat "$root/starts")" -eq 2 ]; then
      case "$FAKE_DOCKER_SCENARIO" in
        released|sha_changed)
          cat "$root/report-$FAKE_DOCKER_SCENARIO.json"
          exit 0
          ;;
      esac
    fi
    cat "$root/report-success.json"
    ;;
  'stop '*|'rm '* )
    rm -f "$root/container"
    ;;
  'logs '* )
    echo 'Synthetic diagnostic without credentials' >&2
    ;;
  *) echo "Unexpected Docker double call: $*" >&2; exit 9 ;;
esac
"""


def _valid_report() -> dict[str, object]:
    return {
        "gate": "held",
        "legacy_comparison": "matching",
        "legacy_file_present": True,
        "safe_to_review_without_spotify_writes": True,
        "ready_for_production": False,
        "checks": {"owner_inventory_valid": True, "inspection_hold_active": True},
        "playlists": [{"spotify_playlist_id": character * 22} for character in "ABCD"],
        "sources": [
            {"id": source}
            for source in (
                "ser",
                "rne",
                "ondacero",
                "abc",
                "cnn",
                "un_news_en",
                "reuters_world",
                "nplus_univision",
                "dw_actualidad",
                "un_news_es",
                "rfi_fr",
                "dlf_news",
                "rmf_fakty",
            )
        ],
        "owner_templates": [{"id": f"template-{i}"} for i in range(7)],
        "historical_covers": [{"id": "artwork", "persisted_and_verified": True}],
        "v11_state_sha256": "a" * 64,
    }


@pytest.fixture
def fake_docker(tmp_path: Path) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    # Exhaust the 30-attempt health deadline without waiting 30 real seconds.
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    for name in ("starts", "inspections"):
        (tmp_path / name).write_text("0\n", encoding="utf-8")
    success = _valid_report()
    released = {**success, "gate": "released"}
    changed = {**success, "v11_state_sha256": "b" * 64}
    for name, report in (
        ("success", success),
        ("released", released),
        ("sha_changed", changed),
    ):
        (tmp_path / f"report-{name}.json").write_text(json.dumps(report), encoding="utf-8")
    return bin_dir, tmp_path, temporary


def _run_cutover(
    fake_docker: tuple[Path, Path, Path], scenario: str
) -> tuple[subprocess.CompletedProcess[str], int, int, list[str]]:
    bin_dir, state, temporary = fake_docker
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": str(bin_dir) + os.pathsep + environment["PATH"],
            "FAKE_DOCKER_DIR": str(state),
            "FAKE_DOCKER_SCENARIO": scenario,
            "TMPDIR": str(temporary),
        }
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "synthetic-image:not-published"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=environment,
    )
    events = (state / "events").read_text(encoding="utf-8").splitlines()
    assert any(event.startswith("volume rm ") for event in events), events
    assert any(event.startswith("rm -f ") for event in events), events
    assert not (state / "volume").exists(), "Disposable Docker volume leaked"
    assert not (state / "container").exists(), "Disposable Docker container leaked"
    assert list(temporary.iterdir()) == [], "Inspection report temporary file leaked"
    assert all(
        " --network none " in f" {event} " for event in events if event.startswith("run ")
    ), "An offline test container was allowed network egress"
    starts = int((state / "starts").read_text(encoding="utf-8"))
    inspections = int((state / "inspections").read_text(encoding="utf-8"))
    return result, starts, inspections, events


def test_cutover_accepts_matching_held_restart_and_cleans_resources(
    fake_docker: tuple[Path, Path, Path],
) -> None:
    result, starts, _, events = _run_cutover(fake_docker, "success")
    assert result.returncode == 0, result.stderr
    assert starts == 2, "Smoke test must start the image twice"
    assert len([event for event in events if event.startswith("exec ")]) == 2
    assert "PASS: four synthetic destinations" in result.stdout


@pytest.mark.parametrize(
    ("scenario", "failure_message"),
    [
        ("released", "durable inspection gate missing"),
        ("sha_changed", "restart rewrote v11"),
        ("timeout", "did not become healthy"),
    ],
)
def test_cutover_fails_closed_and_cleans_resources(
    fake_docker: tuple[Path, Path, Path], scenario: str, failure_message: str
) -> None:
    result, starts, inspections, _events = _run_cutover(fake_docker, scenario)
    assert result.returncode != 0, "Unsafe cutover was incorrectly accepted"
    assert failure_message in result.stderr
    assert starts == (1 if scenario == "timeout" else 2)
    if scenario == "timeout":
        assert inspections >= 30, "Health timeout did not bound the wait"
