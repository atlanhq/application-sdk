"""Guard: only a live, unscoped Renovate sweep may cancel another sweep.

`renovate.yaml` coalesces sweeps (`cancel-in-progress: true`) because the fleet
App's hourly API budget is sized for roughly one full-fleet pass, and two passes
inside one rate-limit hour exhaust it. But "coalesce" is only correct between
runs that are substitutes for each other. Two kinds of run are not:

* a dry run (`dry_run: full` / `lookup`) writes nothing;
* a `repos`-scoped dispatch covers only the repos it lists.

Either one cancelling a live full-fleet sweep leaves the fleet partially swept
with nothing recording that the rest never ran — strictly worse than the queuing
behaviour it replaced. So both must land on a run-unique group instead.

The expressions are lifted verbatim out of the workflow rather than restated
here: a copy would keep passing after someone edited the YAML, which is the
failure mode this file exists to prevent. `&&` binds tighter than `||` in GHA,
so a term one paren out of place is a silently no-op guard that a presence check
would wave through — hence behavioural cases, not a regex.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate, evaluate_operand  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github/workflows/renovate.yaml"

_SHARED_GROUP = "renovate-fleet"
_RUN_ID = "12345"


def _concurrency() -> dict[str, Any]:
    data = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8")) or {}
    block = data.get("concurrency")
    assert isinstance(block, dict), (
        "renovate.yaml has no workflow-level concurrency block; the coalescing "
        "guard this file tests has been removed or restructured"
    )
    return block


def _as_operand(value: Any, contexts: dict[str, Any]) -> Any:
    """Evaluate a concurrency field that may be a literal rather than an expression.

    `group: renovate-fleet` is a plain string, and feeding that to the evaluator
    raises rather than asserting — which is how the regression this file guards
    against actually looks in YAML. Resolving literals to themselves keeps the
    failure a legible "this trigger shares the live group" instead of a parse
    error three frames deep in the evaluator.
    """
    text = str(value).strip()
    if "${{" not in text:
        return text
    return evaluate_operand(text, contexts)


def _as_bool(value: Any, contexts: dict[str, Any]) -> bool:
    """Same, for `cancel-in-progress`, which may be a literal `true`/`false`."""
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if "${{" not in text:
        return text.lower() == "true"
    return evaluate(text, contexts)


def _contexts(dry_run: Any, repos: Any) -> dict[str, Any]:
    return {
        "inputs": {"dry_run": dry_run, "repos": repos},
        "github": {"run_id": _RUN_ID},
    }


@dataclass(frozen=True)
class Trigger:
    """One way the workflow starts, and whether it is a live unscoped sweep."""

    name: str
    dry_run: Any
    repos: Any
    live_unscoped: bool


#: `inputs` is absent on `schedule`, so its properties read as null.
TRIGGERS: tuple[Trigger, ...] = (
    Trigger("schedule (no inputs context)", None, None, True),
    Trigger("release fan-out / default dispatch", "null", "", True),
    Trigger("explicit live dispatch", "null", "", True),
    Trigger("dry run, full", "full", "", False),
    Trigger("dry run, lookup", "lookup", "", False),
    Trigger("scoped pilot dispatch", "null", '["atlanhq/atlan-mysql-app"]', False),
    Trigger("dry run AND scoped", "full", '["atlanhq/atlan-mysql-app"]', False),
)


class TestGroup:
    @pytest.mark.parametrize("trigger", TRIGGERS, ids=lambda t: t.name)
    def test_only_live_unscoped_sweeps_share_the_group(self, trigger: Trigger):
        ctx = _contexts(trigger.dry_run, trigger.repos)
        group = _as_operand(_concurrency()["group"], ctx)
        if trigger.live_unscoped:
            assert group == _SHARED_GROUP
        else:
            assert group != _SHARED_GROUP, (
                f"{trigger.name} shares the live sweep's group, so it can cancel "
                "a full-fleet pass it is not a substitute for"
            )

    def test_aside_group_is_run_unique(self):
        """Two dry runs must not cancel each other either."""
        expr = _concurrency()["group"]
        first = _as_operand(expr, _contexts("full", ""))
        second = _as_operand(
            expr,
            {"inputs": {"dry_run": "full", "repos": ""}, "github": {"run_id": "99999"}},
        )
        assert first != second
        assert _RUN_ID in str(first)


class TestCancelInProgress:
    @pytest.mark.parametrize("trigger", TRIGGERS, ids=lambda t: t.name)
    def test_cancellation_is_enabled_only_for_live_unscoped_sweeps(
        self, trigger: Trigger
    ):
        assert (
            _as_bool(
                _concurrency()["cancel-in-progress"],
                _contexts(trigger.dry_run, trigger.repos),
            )
            is trigger.live_unscoped
        )

    @pytest.mark.parametrize("trigger", TRIGGERS, ids=lambda t: t.name)
    def test_group_and_cancel_flag_agree(self, trigger: Trigger):
        """The two expressions are written separately; pin them to one verdict.

        Cancellation on the shared group is the whole point, and cancellation on
        a run-unique group is inert — but a future edit that changed one
        expression and not the other would be silent, and the failure it
        produces (a pilot aborting a live sweep) is invisible until the fleet is
        half-swept.
        """
        block = _concurrency()
        ctx = _contexts(trigger.dry_run, trigger.repos)
        shared = _as_operand(block["group"], ctx) == _SHARED_GROUP
        assert _as_bool(block["cancel-in-progress"], ctx) is shared

    def test_live_sweeps_still_coalesce(self):
        """The incident fix itself: two live sweeps must not both run."""
        block = _concurrency()
        ctx = _contexts("null", "")
        assert _as_operand(block["group"], ctx) == _SHARED_GROUP
        assert _as_bool(block["cancel-in-progress"], ctx) is True
