#!/usr/bin/env bash
# with-retry.sh — retry a command on non-zero exit with exponential backoff.
#
# Usage:   with-retry.sh <command> [args...]
# Env:     RETRY_MAX_ATTEMPTS         (default 5)     — total attempts; 1 means no retry
#          RETRY_BACKOFF_BASE_SECONDS (default 5)     — first delay; doubles each retry
#          RETRY_BACKOFF_MAX_SECONDS  (default 60)    — per-delay ceiling
#          RETRY_SLEEP_COMMAND        (default sleep) — test seam; see the bats suite
#
# Backoff is exponential (base, 2×base, 4×base, … up to the ceiling) plus up
# to 25% jitter. The defaults give 5+10+20+40 ≈ 75s of cover across 5 attempts.
#
# That ladder is sized against the failure this exists for: github.com release
# -asset outages, which run for minutes, not seconds. The previous linear
# 3-attempt 5+10=15s ladder was short enough that a single 503 burst on
# `apple/pkl` took the Connector Tests Gate — and with it a merge-queue entry —
# down anyway, which is the whole outcome the wrapper was meant to prevent.
#
# Jitter is proportional to the delay, so a fleet of jobs retrying against the
# same degraded endpoint does not resynchronise into a thundering herd, and
# RETRY_BACKOFF_BASE_SECONDS=0 stays exactly 0 (the bats suite relies on that
# to run without sleeping).
#
# Exit code: the exit code of the last (failing) attempt, or 0 on success.
set -uo pipefail
[ "$#" -ge 1 ] || { echo "with-retry: usage: with-retry.sh <command> [args...]" >&2; exit 1; }

max="${RETRY_MAX_ATTEMPTS:-5}"
base="${RETRY_BACKOFF_BASE_SECONDS:-5}"
cap="${RETRY_BACKOFF_MAX_SECONDS:-60}"
sleep_cmd="${RETRY_SLEEP_COMMAND:-sleep}"
attempt=1

while true; do
    "$@" && exit 0
    status=$?
    if [ "$attempt" -ge "$max" ]; then
        echo "with-retry: '$*' failed after ${attempt} attempt(s) (exit ${status})" >&2
        exit "$status"
    fi
    # Clamp the exponent before shifting rather than clamping the result after:
    # a large RETRY_MAX_ATTEMPTS would otherwise shift past the integer width
    # and wrap to 0 or negative, silently turning the backoff off entirely.
    exp=$((attempt - 1))
    [ "$exp" -le 16 ] || exp=16
    delay=$((base << exp))
    [ "$delay" -le "$cap" ] || delay="$cap"
    delay=$((delay + RANDOM % (delay / 4 + 1)))
    echo "with-retry: '$*' failed (exit ${status}); retry $((attempt)) of $((max - 1)) in ${delay}s" >&2
    "$sleep_cmd" "$delay"
    attempt=$((attempt + 1))
done
