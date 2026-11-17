#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
failures=0

run_tenant() {
  local tenant="$1"
  local host="$2"
  local env_file="$ROOT/clients/$tenant.env"

  if [[ ! -r "$env_file" ]]; then
    echo "[$tenant] environment file not found" >&2
    failures=$((failures + 1))
    return
  fi

  (
    # Docker env files are data, not shell scripts; values such as MAIL_FROM
    # may contain spaces and angle brackets. Read only the token we require.
    TASK_RUNNER_TOKEN="$(sed -n 's/^TASK_RUNNER_TOKEN=//p' "$env_file" | tail -n 1 | tr -d '\r')"
    if [[ -z "$TASK_RUNNER_TOKEN" ]]; then
      echo "[$tenant] TASK_RUNNER_TOKEN is not configured" >&2
      exit 1
    fi
    printf 'header = "X-Task-Token: %s"\n' "$TASK_RUNNER_TOKEN" |
      curl --config - --fail --silent --show-error --max-time 120 \
        -X POST "https://$host/tasks/asset-schemes/newsletters"
  ) || failures=$((failures + 1))
}

run_tenant hq hq.cooperativems.com
run_tenant ooucoop ooucoop.cooperativems.com
run_tenant smtcoop smtcoop.cooperativems.com

exit "$failures"
