#!/usr/bin/env bash
# with-retry.sh — retry a command on non-zero exit with linear backoff.
#
# Usage:   with-retry.sh <command> [args...]
# Env:     RETRY_MAX_ATTEMPTS       (default 3)  — total attempts; 1 means no retry
#          RETRY_BACKOFF_BASE_SECONDS (default 5) — sleep = attempt * base
#
# Exit code: the exit code of the last (failing) attempt, or 0 on success.
set -uo pipefail
[ "$#" -ge 1 ] || { echo "with-retry: usage: with-retry.sh <command> [args...]" >&2; exit 1; }

max="${RETRY_MAX_ATTEMPTS:-3}"
base="${RETRY_BACKOFF_BASE_SECONDS:-5}"
attempt=1

while true; do
    "$@" && exit 0
    status=$?
    if [ "$attempt" -ge "$max" ]; then
        echo "with-retry: '$*' failed after ${attempt} attempt(s) (exit ${status})" >&2
        exit "$status"
    fi
    delay=$((attempt * base))
    echo "with-retry: '$*' failed (exit ${status}); retry $((attempt)) of $((max - 1)) in ${delay}s" >&2
    sleep "$delay"
    attempt=$((attempt + 1))
done
