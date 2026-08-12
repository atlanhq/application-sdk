"""The conformance required gate must decide from the suite result AND exit-zero (FND-199).

The `Conformance Gate` job is the required check.  It reads the suite matrix
job's *aggregate* result, which is 'failure' if any leg failed for any reason —
including reasons that graded no rule at all.  The D-series leg is the only one
that materialises the caller's environment, and its `uv sync` dies on a
dependency with no manylinux wheel (python-ldap, mysqlclient, psycopg2 from
source, sasl), on a private-index 401, on a network flake.  detect never runs.

That made the gate asymmetric for soft-enforcement callers: with
`exit-zero: true` a rule *violation* could never fail the gate (detect exits 0)
while a *crash in the same series* still did.  The fix honours `exit-zero` in
the gate as well.  It loosens no rule enforcement — under exit-zero detect
cannot exit nonzero on findings, so a 'failure' aggregate is by construction a
crash, never a verdict.

Two properties are asserted here, and the second is the one that actually
protects the merge queue:

* The decision table itself — notably that 'cancelled' fails in BOTH modes.  A
  cancelled run graded nothing; reporting green from it would let a merge queue
  merge on a check that never ran.  An earlier `contains(..., 'failure')` rule
  had exactly that hole, so it is pinned rather than assumed.
* **Exhaustiveness.**  The branch is three sibling steps whose `if:`s must
  partition the space: exactly one fires for every input.  Miss a case and the
  job runs no step, succeeds, and reports a green required check having
  evaluated nothing — the same silent-pass failure mode in a new place.  A
  textual "is the third condition the negation of the other two" check would
  not survive a reworded expression, so the partition is proven behaviourally
  over the cross product instead.

The conditions are lifted verbatim from the workflow and evaluated with the
repo's GHA expression evaluator, so a change to precedence — `&&` binds tighter
than `||`, and one paren out of place turns the exit-zero term into a no-op —
fails here rather than in production.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github/workflows/conformance-reusable.yaml"

#: Step names, in the order the workflow declares them.  Keyed by the outcome
#: each step represents so the assertions below read as a decision table.
_PASS_CLEAN = "Conformance gate passed"
_PASS_ADVISORY = "Conformance gate passed (advisory — exit-zero)"
_FAIL = "Conformance gate failed"

#: Every value `needs.<job>.result` can take, plus two that it should not:
#: the empty string (a job that did not run) and a value from no known
#: vocabulary.  Both must fail closed rather than fall through the branch.
_SUITE_RESULTS = ("success", "skipped", "failure", "cancelled", "", "neutral")

#: How `inputs.exit-zero` can arrive, and what it MEANS.  A `type: boolean`
#: workflow_call input is coerced to a real boolean, so the first two are what
#: production actually passes.  The string forms are here because a bare
#: `inputs.exit-zero` is a cast-to-boolean and GHA treats the string 'false' as
#: TRUTHY — so if the coercion ever stopped holding, the naive condition would
#: read "false" as "advisory" and every hard-gate caller would silently stop
#: enforcing while its required check went green.  That direction is much worse
#: than the other, hence the `!= 'false'` term in the workflow and these rows.
_EXIT_ZERO_VALUES: tuple[tuple[Any, bool], ...] = (
    (False, False),
    (True, True),
    ("false", False),
    ("true", True),
)

#: (suite result, exit-zero means advisory) -> the single step that must fire.
_EXPECTED: dict[tuple[str, bool], str] = {
    ("success", False): _PASS_CLEAN,
    ("success", True): _PASS_CLEAN,
    ("skipped", False): _PASS_CLEAN,
    ("skipped", True): _PASS_CLEAN,
    # The whole point of FND-199: a crashed leg blocks a hard gate and is
    # tolerated (loudly) under soft enforcement.
    ("failure", False): _FAIL,
    ("failure", True): _PASS_ADVISORY,
    # A cancelled run graded nothing — never green, in either mode.
    ("cancelled", False): _FAIL,
    ("cancelled", True): _FAIL,
    ("", False): _FAIL,
    ("", True): _FAIL,
    ("neutral", False): _FAIL,
    ("neutral", True): _FAIL,
}


@pytest.fixture(scope="module")
def gate_steps() -> dict[str, dict[str, Any]]:
    """The gate job's steps, keyed by name."""
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    gate = workflow["jobs"]["gate"]
    steps = {step["name"]: step for step in gate["steps"]}
    missing = {_PASS_CLEAN, _PASS_ADVISORY, _FAIL} - steps.keys()
    assert not missing, (
        f"gate job is missing step(s) {sorted(missing)} — this test identifies the "
        f"branches by name; rename them here and in the workflow together"
    )
    return steps


def _contexts(suite_result: str, exit_zero: Any) -> dict[str, Any]:
    return {
        "needs": {"suite": {"result": suite_result}},
        "inputs": {"exit-zero": exit_zero},
    }


def _cases() -> Iterator[tuple[str, Any, str]]:
    for suite_result in _SUITE_RESULTS:
        for exit_zero, is_advisory in _EXIT_ZERO_VALUES:
            yield suite_result, exit_zero, _EXPECTED[(suite_result, is_advisory)]


@pytest.mark.parametrize(("suite_result", "exit_zero", "expected"), list(_cases()))
def test_exactly_one_branch_fires(
    gate_steps: dict[str, dict[str, Any]],
    suite_result: str,
    exit_zero: Any,
    expected: str,
) -> None:
    """Every (result × exit-zero) pair selects one branch — never zero, never two.

    Zero is the dangerous direction: the job would succeed having run nothing
    and report a green required check.
    """
    contexts = _contexts(suite_result, exit_zero)
    fired = [
        name for name, step in gate_steps.items() if evaluate(str(step["if"]), contexts)
    ]
    assert fired == [expected], (
        f"suite result {suite_result!r} with exit-zero={exit_zero} fired {fired}, "
        f"expected exactly [{expected!r}]"
    )


def test_only_the_failing_branch_exits_nonzero(
    gate_steps: dict[str, dict[str, Any]],
) -> None:
    """The verdict is the exit code, not the wording of the log line."""
    assert "exit 1" in gate_steps[_FAIL]["run"]
    for name in (_PASS_CLEAN, _PASS_ADVISORY):
        assert (
            "exit 1" not in gate_steps[name]["run"]
        ), f"{name!r} is a passing branch but its script exits nonzero"


def test_advisory_pass_is_announced(gate_steps: dict[str, dict[str, Any]]) -> None:
    """A green gate over a failed leg must say why, where a reader will see it.

    A required check that is green is the one nobody opens, so the job log alone
    does not count: the reason belongs in a PR annotation and the run summary.
    """
    run = gate_steps[_PASS_ADVISORY]["run"]
    assert "::warning" in run, "advisory pass must raise a PR annotation"
    assert "GITHUB_STEP_SUMMARY" in run, "advisory pass must write the run summary"


def test_gate_still_always_runs_after_the_whole_suite() -> None:
    """`if: always()` + `needs: [suite]` is what makes this a viable required check.

    Drop `always()` and the gate goes 'skipped' whenever a leg fails, which in a
    merge queue is indistinguishable from "not required yet" and blocks forever.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    gate = workflow["jobs"]["gate"]
    assert str(gate["if"]).strip() == "always()"
    assert gate["needs"] == ["suite"]
