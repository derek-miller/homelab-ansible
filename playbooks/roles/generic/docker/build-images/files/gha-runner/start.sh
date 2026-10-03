#!/bin/bash
# Registers an ephemeral runner, takes one job and exits; swarm then starts a
# clean task, which registers again.
set -euo pipefail

# Swarm restarts a failed task within seconds. Waiting a minute first keeps a bad
# token from hammering the API while still tripping the crash-loop alert.
fail() {
  echo "$1" >&2
  sleep 60
  exit 1
}

if ! response=$(curl -sS --fail-with-body -X POST \
  -H "Authorization: Bearer ${GITHUB_PAT}" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/repos/${RUNNER_REPOSITORY}/actions/runners/registration-token"); then
  fail "no registration token for ${RUNNER_REPOSITORY}: ${response}"
fi
# Jobs inherit the runner's environment, and this token administers the repository.
unset GITHUB_PAT

./config.sh --unattended --ephemeral --replace \
  --url "https://github.com/${RUNNER_REPOSITORY}" \
  --token "$(jq -r .token <<<"$response")" \
  --name "$RUNNER_NAME" \
  --labels "$RUNNER_LABELS" \
  --work _work \
  || fail "registering ${RUNNER_NAME} on ${RUNNER_REPOSITORY} failed"

exec ./run.sh
