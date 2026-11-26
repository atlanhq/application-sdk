"""Tests for .github/scripts/with-retry.sh.

Was a bats suite that needed its own CI job, a setup-deps run and a pinned
bats-core checkout for one file. It runs here, in ``pytest .github/scripts/tests``,
by driving the script through ``bash`` (FND-4673).

The helpers the bats suite exported as shell functions (``fail_n_times``,
``record_sleep``) are small executables on ``PATH`` instead: ``with-retry.sh``
runs its command and ``RETRY_SLEEP_COMMAND`` as plain commands, so a file on
``PATH`` is found the same way an exported function was.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "with-retry.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="with-retry.sh needs bash"
)

# fail_n_times <n> <counter_file>: fails the first n calls, succeeds after.
_FAIL_N_TIMES = """#!/usr/bin/env bash
n="$1"; counter_file="$2"
count=$(cat "$counter_file" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$counter_file"
[ "$count" -gt "$n" ]
"""

# record_sleep <seconds>: stands in for `sleep` via RETRY_SLEEP_COMMAND so the
# backoff ladder can be asserted with a realistic base and no wall-clock cost.
_RECORD_SLEEP = """#!/usr/bin/env bash
echo "$1" >> "$SLEEP_LOG"
"""

Run = Callable[..., subprocess.CompletedProcess[str]]


def _install(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def counter(tmp_path: Path) -> Path:
    return tmp_path / "counter"


@pytest.fixture
def sleep_log(tmp_path: Path) -> Path:
    path = tmp_path / "sleep.log"
    path.touch()
    return path


@pytest.fixture
def run(tmp_path: Path, sleep_log: Path) -> Run:
    """Run with-retry.sh with the helpers on PATH and no real sleeping.

    ``env`` overrides are applied on top of a base that sets the backoff to 0;
    an override of ``None`` unsets the variable, to exercise its default.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install(bin_dir, "fail_n_times", _FAIL_N_TIMES)
    _install(bin_dir, "record_sleep", _RECORD_SLEEP)

    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("RETRY_")  # a developer's own settings must not leak in
    }
    base["PATH"] = f"{bin_dir}{os.pathsep}{base.get('PATH', '')}"
    base["SLEEP_LOG"] = str(sleep_log)
    base["RETRY_BACKOFF_BASE_SECONDS"] = "0"

    def _run(*argv: str, **env: str | None) -> subprocess.CompletedProcess[str]:
        merged = dict(base)
        for key, value in env.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return subprocess.run(
            ["bash", str(SCRIPT), *argv],
            env=merged,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    return _run


def _calls(counter: Path) -> int:
    return int(counter.read_text().strip())


def _delays(sleep_log: Path) -> list[int]:
    return [int(line) for line in sleep_log.read_text().split()]


def _assert_delay(actual: int, floor: int) -> None:
    """At least ``floor`` and at most ``floor`` + 25%, the script's jitter band.

    The ceiling is integer-truncated the same way the script computes
    ``delay / 4``.
    """
    assert floor <= actual <= floor + floor // 4, (actual, floor)


# ---------- basic behaviour ---------------------------------------------------


def test_no_arguments_exits_1_with_usage(run: Run) -> None:
    result = run()
    assert result.returncode == 1
    assert "usage" in result.stderr


def test_succeeds_on_first_try(run: Run) -> None:
    assert run("true", RETRY_MAX_ATTEMPTS="3").returncode == 0


def test_fails_twice_then_succeeds(run: Run, counter: Path) -> None:
    result = run("fail_n_times", "2", str(counter), RETRY_MAX_ATTEMPTS="3")
    assert result.returncode == 0
    assert _calls(counter) == 3


def test_always_fails_exits_with_the_command_exit_code(run: Run) -> None:
    assert run("false", RETRY_MAX_ATTEMPTS="3").returncode == 1


def test_always_fails_is_invoked_exactly_max_attempts_times(
    run: Run, counter: Path
) -> None:
    run("fail_n_times", "99", str(counter), RETRY_MAX_ATTEMPTS="3")
    assert _calls(counter) == 3


def test_propagates_a_non_1_exit_code_unchanged(run: Run) -> None:
    assert run("bash", "-c", "exit 42", RETRY_MAX_ATTEMPTS="1").returncode == 42


def test_max_attempts_1_means_no_retry(run: Run, counter: Path) -> None:
    result = run("fail_n_times", "1", str(counter), RETRY_MAX_ATTEMPTS="1")
    assert result.returncode != 0
    assert _calls(counter) == 1


def test_max_attempts_1_and_success_exits_0(run: Run) -> None:
    assert run("true", RETRY_MAX_ATTEMPTS="1").returncode == 0


def test_succeeds_on_second_try_with_max_attempts_2(run: Run, counter: Path) -> None:
    result = run("fail_n_times", "1", str(counter), RETRY_MAX_ATTEMPTS="2")
    assert result.returncode == 0
    assert _calls(counter) == 2


# ---------- backoff ladder ----------------------------------------------------
#
# The ladder is the point of the wrapper: a 15s ladder does not survive the
# minutes-long github.com release-asset outages this exists for. Pin the shape
# so a future edit cannot quietly shorten it back.


def test_default_max_attempts_is_5(run: Run, counter: Path) -> None:
    result = run("fail_n_times", "99", str(counter), RETRY_MAX_ATTEMPTS=None)
    assert result.returncode != 0
    assert _calls(counter) == 5


def test_backoff_is_exponential_capped_and_jittered(run: Run, sleep_log: Path) -> None:
    result = run(
        "false",
        RETRY_BACKOFF_BASE_SECONDS=None,  # exercise the real default (5s)
        RETRY_MAX_ATTEMPTS="6",
        RETRY_SLEEP_COMMAND="record_sleep",
    )
    assert result.returncode == 1

    # 6 attempts -> 5 delays: 5, 10, 20, 40, then capped at 60 (not 80).
    delays = _delays(sleep_log)
    assert len(delays) == 5
    for actual, floor in zip(delays, [5, 10, 20, 40, 60], strict=True):
        _assert_delay(actual, floor)


def test_base_0_sleeps_exactly_0(run: Run, sleep_log: Path) -> None:
    # Jitter is proportional to the delay, so it never rounds 0 up.
    result = run(
        "false",
        RETRY_BACKOFF_BASE_SECONDS="0",
        RETRY_MAX_ATTEMPTS="4",
        RETRY_SLEEP_COMMAND="record_sleep",
    )
    assert result.returncode == 1
    assert _delays(sleep_log) == [0, 0, 0]


def test_max_seconds_caps_the_delay(run: Run, sleep_log: Path) -> None:
    result = run(
        "false",
        RETRY_BACKOFF_BASE_SECONDS=None,
        RETRY_BACKOFF_MAX_SECONDS="7",
        RETRY_MAX_ATTEMPTS="4",
        RETRY_SLEEP_COMMAND="record_sleep",
    )
    assert result.returncode == 1

    # 5 is below the cap; 10 and 20 clamp to 7. Jitter tops out at +25%.
    delays = _delays(sleep_log)
    assert len(delays) == 3
    for actual, floor in zip(delays, [5, 7, 7], strict=True):
        _assert_delay(actual, floor)


def test_huge_max_attempts_never_overflows_into_a_zero_delay(
    run: Run, sleep_log: Path
) -> None:
    result = run(
        "false",
        RETRY_BACKOFF_BASE_SECONDS=None,
        RETRY_MAX_ATTEMPTS="70",
        RETRY_SLEEP_COMMAND="record_sleep",
    )
    assert result.returncode == 1

    delays = _delays(sleep_log)
    assert len(delays) == 69
    # Every delay from the fifth on sits at the 60s ceiling; none collapse to 0.
    for actual in delays[4:]:
        _assert_delay(actual, 60)
