#!/usr/bin/env bats
# Tests for .github/scripts/with-retry.sh
#
# Run:  bats .github/scripts/tests/with-retry.bats
# Req:  bats-core >= 1.5 (https://github.com/bats-core/bats-core)

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

# record_sleep <seconds> — stands in for `sleep` via RETRY_SLEEP_COMMAND so the
# backoff ladder can be asserted with a realistic base and no wall-clock cost.
record_sleep() {
    echo "$1" >> "$SLEEP_LOG"
}
export -f record_sleep

# delay_count / nth_delay <n> — read back what record_sleep captured.
# Deliberately not `mapfile`: it is a bash 4 builtin, and macOS still ships
# bash 3.2 as /bin/bash, so a dev running this suite locally would see the
# ladder tests error out for reasons that have nothing to do with the ladder.
delay_count() {
    wc -l < "$SLEEP_LOG" | tr -d ' '
}

nth_delay() {
    sed -n "$1p" "$SLEEP_LOG"
}

# assert_delay <n> <floor> — the nth delay is at least floor and no more than
# floor +25%, the jitter band the script applies. The ceiling is integer
# -truncated the same way the script computes `delay / 4`.
assert_delay() {
    local actual
    actual="$(nth_delay "$1")"
    [ "$actual" -ge "$2" ]
    [ "$actual" -le "$(( $2 + $2 / 4 ))" ]
}

setup() {
    COUNTER_FILE="$(mktemp)"
    SLEEP_LOG="$(mktemp)"
    export SLEEP_LOG
    # No sleep in tests — override the backoff to 0.
    export RETRY_BACKOFF_BASE_SECONDS=0
}

teardown() {
    rm -f "$COUNTER_FILE" "$SLEEP_LOG"
}

# ---------- tests -------------------------------------------------------------

@test "no arguments — exits 1 with usage message" {
    run bash "$SCRIPT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"usage"* ]]
}

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

# ---------- backoff ladder ----------------------------------------------------
#
# The ladder is the point of the wrapper: a 15s ladder does not survive the
# minutes-long github.com release-asset outages this exists for. Pin the shape
# so a future edit cannot quietly shorten it back.

@test "default RETRY_MAX_ATTEMPTS is 5" {
    unset RETRY_MAX_ATTEMPTS
    run bash "$SCRIPT" bash -c "fail_n_times 99 '$COUNTER_FILE'"
    [ "$status" -ne 0 ]
    [ "$(cat "$COUNTER_FILE")" -eq 5 ]
}

@test "backoff is exponential, capped, and jittered by at most 25%" {
    unset RETRY_BACKOFF_BASE_SECONDS   # exercise the real default (5s)
    export RETRY_MAX_ATTEMPTS=6
    export RETRY_SLEEP_COMMAND=record_sleep

    run bash "$SCRIPT" false
    [ "$status" -eq 1 ]

    # 6 attempts → 5 delays: 5, 10, 20, 40, then capped at 60 (not 80).
    [ "$(delay_count)" -eq 5 ]
    assert_delay 1 5
    assert_delay 2 10
    assert_delay 3 20
    assert_delay 4 40
    assert_delay 5 60
}

@test "RETRY_BACKOFF_BASE_SECONDS=0 sleeps exactly 0 — jitter never rounds up" {
    export RETRY_BACKOFF_BASE_SECONDS=0
    export RETRY_MAX_ATTEMPTS=4
    export RETRY_SLEEP_COMMAND=record_sleep

    run bash "$SCRIPT" false
    [ "$status" -eq 1 ]

    [ "$(delay_count)" -eq 3 ]
    [ "$(nth_delay 1)" -eq 0 ]
    [ "$(nth_delay 2)" -eq 0 ]
    [ "$(nth_delay 3)" -eq 0 ]
}

@test "RETRY_BACKOFF_MAX_SECONDS caps the delay" {
    unset RETRY_BACKOFF_BASE_SECONDS
    export RETRY_BACKOFF_MAX_SECONDS=7
    export RETRY_MAX_ATTEMPTS=4
    export RETRY_SLEEP_COMMAND=record_sleep

    run bash "$SCRIPT" false
    [ "$status" -eq 1 ]

    [ "$(delay_count)" -eq 3 ]
    # 5 is below the cap; 10 and 20 clamp to 7. Jitter tops out at +25%.
    assert_delay 1 5
    assert_delay 2 7
    assert_delay 3 7
}

@test "a huge RETRY_MAX_ATTEMPTS never overflows the shift into a zero delay" {
    unset RETRY_BACKOFF_BASE_SECONDS
    export RETRY_MAX_ATTEMPTS=70
    export RETRY_SLEEP_COMMAND=record_sleep

    run bash "$SCRIPT" false
    [ "$status" -eq 1 ]

    [ "$(delay_count)" -eq 69 ]
    # Every delay from the fifth on sits at the 60s ceiling; none collapse to 0.
    local n
    for n in $(seq 5 69); do
        assert_delay "$n" 60
    done
}
