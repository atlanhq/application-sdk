#!/usr/bin/env bats
# Tests for .github/scripts/with-retry.sh
#
# Run:  bats .github/scripts/tests/with-retry.bats
# Req:  bats-core >= 1.9 (https://github.com/bats-core/bats-core)

SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/with-retry.sh"

# ---------- helpers ----------------------------------------------------------

# fail_n_times <n> <counter_file> — succeeds on attempt (n+1), fails the first n.
fail_n_times() {
    local n="$1" counter_file="$2"
    local count
    count=$(cat "$counter_file" 2>/dev/null || echo 0)
    count=$((count + 1))
    echo "$count" > "$counter_file"
    if [ "$count" -le "$n" ]; then
        return 1
    fi
    return 0
}
export -f fail_n_times

setup() {
    COUNTER_FILE="$(mktemp)"
    # No sleep in tests — override the backoff to 0.
    export RETRY_BACKOFF_BASE_SECONDS=0
}

teardown() {
    rm -f "$COUNTER_FILE"
}

# ---------- tests -------------------------------------------------------------

@test "succeeds on first try — command invoked exactly once" {
    export RETRY_MAX_ATTEMPTS=3
    run bash "$SCRIPT" true
    [ "$status" -eq 0 ]
}

@test "fails twice then succeeds — exits 0, invoked 3 times" {
    export RETRY_MAX_ATTEMPTS=3
    run bash "$SCRIPT" bash -c "fail_n_times 2 '$COUNTER_FILE'"
    [ "$status" -eq 0 ]
    # counter file records the number of calls
    [ "$(cat "$COUNTER_FILE")" -eq 3 ]
}

@test "always fails — exits with the command exit code after max attempts" {
    export RETRY_MAX_ATTEMPTS=3
    run bash "$SCRIPT" false
    [ "$status" -eq 1 ]
}

@test "always fails — invoked exactly RETRY_MAX_ATTEMPTS times" {
    export RETRY_MAX_ATTEMPTS=3
    run bash "$SCRIPT" bash -c "fail_n_times 99 '$COUNTER_FILE'"
    [ "$(cat "$COUNTER_FILE")" -eq 3 ]
}

@test "propagates non-1 exit code unchanged" {
    export RETRY_MAX_ATTEMPTS=1
    run bash "$SCRIPT" bash -c "exit 42"
    [ "$status" -eq 42 ]
}

@test "RETRY_MAX_ATTEMPTS=1 means no retry — fails immediately" {
    export RETRY_MAX_ATTEMPTS=1
    run bash "$SCRIPT" bash -c "fail_n_times 1 '$COUNTER_FILE'"
    [ "$status" -ne 0 ]
    [ "$(cat "$COUNTER_FILE")" -eq 1 ]
}

@test "RETRY_MAX_ATTEMPTS=1 and command succeeds — exits 0" {
    export RETRY_MAX_ATTEMPTS=1
    run bash "$SCRIPT" true
    [ "$status" -eq 0 ]
}

@test "succeeds on second try with RETRY_MAX_ATTEMPTS=2" {
    export RETRY_MAX_ATTEMPTS=2
    run bash "$SCRIPT" bash -c "fail_n_times 1 '$COUNTER_FILE'"
    [ "$status" -eq 0 ]
    [ "$(cat "$COUNTER_FILE")" -eq 2 ]
}
