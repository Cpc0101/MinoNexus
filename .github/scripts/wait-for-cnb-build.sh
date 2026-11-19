#!/usr/bin/env bash
set -Eeuo pipefail

: "${CNB_REPOSITORY:?CNB_REPOSITORY is required}"
: "${CNB_BUILD_SN:?CNB_BUILD_SN is required}"
: "${CNB_API_TOKEN:?CNB_API_TOKEN is required}"

timeout_seconds=${CNB_BUILD_TIMEOUT_SECONDS:-7200}
poll_seconds=${CNB_BUILD_POLL_SECONDS:-15}
case "$timeout_seconds" in
  ''|*[!0-9]*) echo "Invalid CNB_BUILD_TIMEOUT_SECONDS: $timeout_seconds" >&2; exit 1 ;;
esac
case "$poll_seconds" in
  ''|*[!0-9]*) echo "Invalid CNB_BUILD_POLL_SECONDS: $poll_seconds" >&2; exit 1 ;;
esac
(( timeout_seconds > 0 )) || { echo 'CNB_BUILD_TIMEOUT_SECONDS must be positive' >&2; exit 1; }
(( poll_seconds > 0 )) || { echo 'CNB_BUILD_POLL_SECONDS must be positive' >&2; exit 1; }

response_file=$(mktemp)
trap 'rm -f "$response_file"' EXIT
deadline=$((SECONDS + timeout_seconds))
last_status=
request_failures=0
url="https://api.cnb.cool/$CNB_REPOSITORY/-/build/status/$CNB_BUILD_SN"

while (( SECONDS < deadline )); do
  if curl --fail-with-body --silent --show-error \
    --connect-timeout 20 \
    --max-time 60 \
    --request GET \
    --url "$url" \
    --header 'Accept: application/vnd.cnb.api+json' \
    --header "Authorization: $CNB_API_TOKEN" \
    --output "$response_file"; then
    request_failures=0
    status=$(jq -r '.status // .data.status // empty' "$response_file")
    if [[ -z "$status" ]]; then
      echo 'CNB status response did not contain a status field.' >&2
      jq -r 'del(.token, .access_token) | .  # keep diagnostics bounded' "$response_file" >&2 || true
      exit 1
    fi
    normalized_status=$(printf '%s' "$status" | tr '[:upper:]' '[:lower:]')

    if [[ "$normalized_status" != "$last_status" ]]; then
      echo "CNB build $CNB_BUILD_SN status: $status"
      last_status=$normalized_status
    fi

    case "$normalized_status" in
      success|succeeded|passed)
        active=$(jq -r '
          [((.pipelinesStatus // .data.pipelinesStatus // {}) | to_entries[]?.value.stages[]?.status // empty)
           | select(. == "pending" or . == "queued" or . == "running" or . == "waiting" or . == "preparing" or . == "starting")]
          | length
        ' "$response_file")
        if [[ "$active" = 0 ]]; then
          echo "CNB build $CNB_BUILD_SN completed successfully."
          exit 0
        fi
        ;;
      error|failed|failure|cancelled|canceled|timeout|timed_out|skipped)
        jq -r '
          "CNB build \(.sn // .data.sn // "unknown") failed with status \(.status // .data.status // "unknown").",
          (((.pipelinesStatus // .data.pipelinesStatus // {}) | to_entries[]?) |
            "  pipeline \(.key): \(.value.status // "unknown")",
            ((.value.stages // [])[]? |
              "    stage \(.name // .id // "unknown"): \(.status // "unknown")"))
        ' "$response_file" >&2 || true
        echo "CNB build failed: $CNB_BUILD_SN" >&2
        exit 1
        ;;
    esac
  else
    request_failures=$((request_failures + 1))
    echo "Unable to query CNB build status (attempt $request_failures)." >&2
    if (( request_failures >= 5 )); then
      echo "CNB status endpoint failed $request_failures consecutive times." >&2
      exit 1
    fi
  fi

  remaining=$((deadline - SECONDS))
  (( remaining > 0 )) || break
  if (( remaining < poll_seconds )); then
    sleep "$remaining"
  else
    sleep "$poll_seconds"
  fi
done

echo "Timed out after ${timeout_seconds}s waiting for CNB build $CNB_BUILD_SN." >&2
exit 1
