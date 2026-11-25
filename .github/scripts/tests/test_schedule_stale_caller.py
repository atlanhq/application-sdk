"""Guard: application-sdk's own stale job labels for real and never closes (FND-4930).

``schedule.yaml``'s ``stale`` job ran ``actions/stale`` with ``debug-only: true``
from the initial commit until FND-4930: it minted a token every night and
labelled nothing, and nobody noticed for 19 months. It now calls the reusable
``stale.yml`` with ``debug-only: false``. These tests pin both halves of that
decision, so neither a quiet flip back to a dry run nor a positive
``days-before-close`` (which would close this repo's backlog on the next run)
can land without editing a test that names the reason.
"""

from __future__ import annotations

from typing import Any

import pytest
from _workflows import WORKFLOWS, job, load_workflow

Doc = dict[str, Any]

REUSABLE = "./.github/workflows/stale.yml"


def _inputs(reusable: Doc) -> Doc:
    # PyYAML reads a bare ``on:`` key as the boolean True, not the string "on".
    on_key: Any = True
    trigger: Doc = reusable.get("on") or reusable[on_key]
    return trigger["workflow_call"]["inputs"]


def stale_caller_problems(caller: Doc, reusable: Doc) -> list[str]:
    """Why ``caller`` (the schedule.yaml ``stale`` job) would not label-only."""
    problems: list[str] = []
    if caller.get("uses") != REUSABLE:
        problems.append(f"uses {caller.get('uses')!r}, not {REUSABLE!r}")
    with_: Doc = caller.get("with") or {}
    if with_.get("debug-only") is not False:
        problems.append(
            f"debug-only is {with_.get('debug-only')!r}; it must be false, or the "
            "job is a dry run that labels nothing"
        )
    inputs: Doc = _inputs(reusable)
    close = with_.get("days-before-close", inputs["days-before-close"]["default"])
    if close != -1:
        problems.append(
            f"days-before-close resolves to {close!r}; it must stay -1 (never "
            "close), or the next run closes this repo's stale backlog"
        )
    return problems


def test_schedule_stale_labels_and_never_closes() -> None:
    caller = job(load_workflow(WORKFLOWS / "schedule.yaml"), "stale")
    reusable = load_workflow(WORKFLOWS / "stale.yml")
    assert not stale_caller_problems(caller, reusable)


@pytest.mark.parametrize(
    "with_",
    [
        {"debug-only": True},
        {},
        {"debug-only": "false"},
        {"debug-only": False, "days-before-close": 7},
    ],
    ids=["dry-run", "default-dry-run", "string-false", "closes"],
)
def test_detects_a_dry_run_or_closing_caller(with_: Doc) -> None:
    """Red-green: each shape this guard exists to stop is reported."""
    reusable = load_workflow(WORKFLOWS / "stale.yml")
    assert stale_caller_problems({"uses": REUSABLE, "with": with_}, reusable)


def test_detects_a_reusable_default_that_closes() -> None:
    reusable = load_workflow(WORKFLOWS / "stale.yml")
    _inputs(reusable)["days-before-close"]["default"] = 30
    caller = {"uses": REUSABLE, "with": {"debug-only": False}}
    assert stale_caller_problems(caller, reusable)
