#!/usr/bin/env bash
# Offline smoke test of the *packaged* Docker image's forward-only v10 -> v11
# migration. It uses a throwaway Docker volume and synthetic Spotify IDs.
# No Spotify, GHCR publication, OAuth, or production /data access is permitted.
set -euo pipefail

if [[ "$#" -ne 1 || -z "$1" ]]; then
  echo "Usage: bash scripts/verify-v11-container-cutover.sh <local-image-tag>" >&2
  exit 2
fi

image="$1"
volume="nbp-cutover-$RANDOM-$$"
container="nbp-cutover-$RANDOM-$$"
first_report="$(mktemp)"
second_report="$(mktemp)"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -f "$first_report" "$second_report"
}
trap cleanup EXIT

docker volume create "$volume" >/dev/null

# Seed four synthetic 22-character destination IDs in schema v10 *without*
# starting the app. No mounted production state, token, or real Spotify ID.
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --mount "type=volume,src=$volume,dst=/data" \
  --entrypoint python "$image" -c '
from dataclasses import replace
from pathlib import Path
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_state import ManagedState, ManagedStateStore, activate_template

identities = {
    "spain_spanish_news",
    "international_english_news",
    "international_polish_news",
    "international_spanish_long_news",
}
templates = tuple(t for t in BUILTIN_CATALOG.playlists if str(t.id) in identities)
assert len(templates) == 4, "expected four built-in synthetic templates"
playlists = tuple(
    replace(
        activate_template(template, chr(65 + index) * 22),
        enabled=index != 2,
        source_ids=template.default_source_ids,
    )
    for index, template in enumerate(templates)
)
ManagedStateStore(Path("/data/managed-state.json")).save(
    ManagedState(schema_version=10, playlists=playlists)
)
'

start_held() {
  if [[ "$1" == first ]]; then
    docker run -d --name "$container" --network none \
      --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
      --cap-drop ALL --security-opt no-new-privileges:true \
      --env NEWS_PLAYLIST_STARTUP_INSPECTION=1 \
      --mount "type=volume,src=$volume,dst=/data" "$image" >/dev/null
  else
    # Deliberately remove the env flag: the durable hold MUST survive restart.
    docker run -d --name "$container" --network none \
      --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
      --cap-drop ALL --security-opt no-new-privileges:true \
      --mount "type=volume,src=$volume,dst=/data" "$image" >/dev/null
  fi

  for attempt in $(seq 1 30); do
    state="$(docker inspect --format '{{.State.Health.Status}}' "$container")"
    if [[ "$state" == healthy ]]; then
      return 0
    fi
    if [[ "$state" == unhealthy ]]; then
      docker logs "$container" >&2
      return 1
    fi
    sleep 1
  done
  docker logs "$container" >&2
  echo "ERROR: held cutover container did not become healthy" >&2
  return 1
}

verify_report() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
baseline = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
assert report["gate"] == "held", "durable inspection gate missing"
assert report["legacy_comparison"] == "matching", "original policy changed"
assert report["legacy_file_present"] is True
assert report["safe_to_review_without_spotify_writes"] is True
assert report["ready_for_production"] is False
assert all(report["checks"].values()), report["checks"]
playlists = report["playlists"]
assert len(playlists) == 4, "must preserve all four destination records"
assert {row["spotify_playlist_id"] for row in playlists} == {
    character * 22 for character in "ABCD"
}, "changed, lost, or duplicated Spotify destinations"
assert len(report["sources"]) >= 13, "original sources were lost"
assert {row["id"] for row in report["sources"]} >= {"ser", "rne", "ondacero"}
assert len(report["owner_templates"]) >= 7, "original templates were lost"
assert report["historical_covers"], "historic cover inventory missing"
assert all(row["persisted_and_verified"] for row in report["historical_covers"])
assert report["v11_state_sha256"] == baseline["v11_state_sha256"], "restart rewrote v11"
assert report["sources"] == baseline["sources"], "source definitions changed on restart"
assert report["playlists"] == baseline["playlists"], "playlist policies changed on restart"
assert report["owner_templates"] == baseline["owner_templates"]
print("PASS: four synthetic destinations, owner sources, templates, inherited artwork,")
print("      held/matching report, no external network and durable restart.")
PY
}

start_held first
docker exec "$container" news-playlist inspection-report --data-dir /data > "$first_report"
verify_report "$first_report" "$first_report"

docker stop "$container" >/dev/null
docker rm "$container" >/dev/null
start_held restart
docker exec "$container" news-playlist inspection-report --data-dir /data > "$second_report"
verify_report "$second_report" "$first_report"
