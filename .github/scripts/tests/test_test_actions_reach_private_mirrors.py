"""The connector test actions run pytest against the private-dep mirrors.

setup-deps mirrors an app's private atlanhq git dependencies, and only a
command started through ``git_auth.py run`` sees the token-free rewrite to those
mirrors. A test that starts its own ``uv sync`` (one that runs a poe task in a
copy of the project, say) inherits pytest's environment. If pytest was started
plain, that sync has no route to the private repos, and it fails with
"Permission denied (publickey)" whenever uv's cache lacks the build. That makes
the test pass or fail depending on the cache.
"""

from __future__ import annotations

import re

import pytest
from _workflows import ACTIONS, load_workflow, step_named

_GIT_AUTH_RUN = 'python3 "${{ github.action_path }}/../../scripts/git_auth.py" run -- '


def _logical_lines(script: str) -> list[str]:
    """The script's shell lines with backslash continuations joined."""
    joined = re.sub(r"\\\n\s*", "", script)
    return [line.strip() for line in joined.splitlines()]


@pytest.mark.parametrize(
    "action, step",
    [
        ("connector-unit-tests", "Run unit tests with coverage"),
        (
            "connector-integration-tests",
            "Run integration tests (parallel via pytest-xdist)",
        ),
    ],
)
def test_pytest_runs_through_git_auth_run(action: str, step: str) -> None:
    script = step_named(load_workflow(ACTIONS / action / "action.yaml"), step)["run"]
    pytest_lines = [
        line
        for line in _logical_lines(script)
        if re.search(r"\bpytest\b", line) and not line.startswith("#")
    ]
    assert pytest_lines, f"no pytest command found in {action}'s {step!r} step"
    for line in pytest_lines:
        assert line.startswith(_GIT_AUTH_RUN + "uv run "), (
            f"{action} starts pytest outside `git_auth.py run`, so tests that "
            f"sync uv themselves cannot reach private deps: {line[:120]}"
        )
