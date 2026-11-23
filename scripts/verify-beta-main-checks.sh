#!/usr/bin/env bash
# Require independent checks on the exact integrated main SHA before beta.
# This script reads GitHub Actions metadata only. No image or Spotify writes.
set -euo pipefail

if [[ "$#" -ne 1 || ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'ERROR: expected exact 40-character source SHA' >&2
  exit 2
fi
if [[ ! "${GITHUB_REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo 'ERROR: invalid GitHub repository context' >&2
  exit 2
fi
if [[ -z "${GH_TOKEN:-}" ]]; then
  echo 'ERROR: GitHub Actions read token is required' >&2
  exit 2
fi

source_sha="$1"
for specification in 'ci.yml push' 'container.yml workflow_dispatch'; do
  read -r workflow expected_event <<< "$specification"
  # Validate returned SHA, branch and event, not merely API query filters.
  # GitHub lists newest first: a later failing run trumps earlier success.
  if ! response="$(gh api --method GET \
    "repos/${GITHUB_REPOSITORY}/actions/workflows/${workflow}/runs" \
    -f "head_sha=${source_sha}" -f branch=main \
    -f "event=${expected_event}" -f per_page=100)"; then
    echo "ERROR: could not check ${workflow} on ${source_sha}" >&2
    exit 1
  fi
  if ! jq -e --arg sha "$source_sha" --arg event "$expected_event" '
    if (.workflow_runs | type) != "array" then false
    else
      [.workflow_runs[] | select(
        .head_sha == $sha and .head_branch == "main" and .event == $event
      )] | if length == 0 then false
        else .[0].status == "completed" and .[0].conclusion == "success"
        end
    end
  ' <<< "$response" >/dev/null; then
    echo "ERROR: ${workflow} ${expected_event} not green on exact main ${source_sha}" >&2
    exit 1
  fi
  echo "PASS: ${workflow} ${expected_event} green on main ${source_sha}"
done
