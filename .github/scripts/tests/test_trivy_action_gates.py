"""Guards for the reporting/enforcement split in .github/actions/trivy (FND-256).

`Trivy Code Scan` is a required status check, and the composite action behind it
does two unrelated things: it *reports* findings (markdown render → PR comment)
and it *enforces* them (a second scan with `exit-code: 1`). Both used to hang off
a single `add-report-comment-to-pr` input, which produced two distinct failures:

1. A transient GitHub API/TLS error while posting a decorative comment failed the
   required check, which ejects the entry from the merge queue and discards every
   other check in it. That is what happened to #3105.
2. The obvious fix — turn commenting off outside `pull_request` — would also have
   turned off the vulnerability and secret gates, silently. Nothing would have
   gone red; the check would simply have stopped checking.

So the contract this file pins is a separation, not a behaviour: reporting may be
skipped and may fail harmlessly — every reporting step, from Python setup to the
comment post, is `continue-on-error` — while enforcement may do neither. Both
halves are GitHub-evaluated `if:` expressions and `continue-on-error` flags that
no runner is available to exercise here, so each gate is lifted verbatim out of
the YAML and evaluated against synthetic contexts — the same approach as
test_label_trigger_gates.py, and for the same reason: a presence check proves a
term is there, not that it is wired in at the right precedence.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ACTION = _REPO_ROOT / ".github/actions/trivy/action.yaml"
_CALLER = _REPO_ROOT / ".github/workflows/pull_request.yaml"

#: Steps that exist only to produce the PR comment. Skippable, and harmless if
#: they fail.
_REPORTING_STEPS = (
    "Set up Python",
    "Install Dependencies",
    "Convert Vulnerability Scan Results to Markdown",
    "Convert Secret Scan Results to Markdown",
    "Comment on PR with Vulnerability Scan Results",
    "Comment on PR with Secret Scan Results",
)

#: The steps that actually gate the check. Neither skippable by event nor
#: allowed to swallow its own failure.
_ENFORCEMENT_STEPS = (
    "Fail on High/Critical Vulnerabilities (with fix available)",
    "Fail on Any Secrets Found",
)

#: The steps whose failure must not fail the job: all of reporting. The comment
#: posts are the FND-256 case; the four preparation steps share the failure
#: shape one step earlier (a `pip install` blip would eject the PR before the
#: enforcement scans even run), so the invariant pins them too.
_NON_FATAL_STEPS = _REPORTING_STEPS

#: Events that reach the trivy job. `merge_group` is the one that motivated
#: FND-256; `pull_request` is where the comment is actually wanted.
_EVENTS = ("pull_request", "merge_group")


def _action() -> dict[str, Any]:
    return yaml.safe_load(_ACTION.read_text(encoding="utf-8"))


def _steps() -> list[dict[str, Any]]:
    return _action()["runs"]["steps"]


def _step(name: str) -> dict[str, Any]:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"{_ACTION.name} has no step named {name!r}")


def _gate(name: str) -> str:
    gate = _step(name).get("if")
    assert gate, f"step {name!r} lost its `if:` gate"
    return str(gate)


def _contexts(*, event: str, comment: str, fail: str) -> dict[str, Any]:
    return {
        "github": {"event_name": event},
        "inputs": {
            "add-report-comment-to-pr": comment,
            "fail-on-findings": fail,
        },
    }


# ── Reporting is skipped outside pull_request ────────────────────────────────


@pytest.mark.parametrize("name", _REPORTING_STEPS)
@pytest.mark.parametrize(
    "event, comment, expected",
    [
        ("pull_request", "true", True),
        ("pull_request", "false", False),
        ("merge_group", "true", False),
        ("merge_group", "false", False),
    ],
)
def test_reporting_runs_only_on_pull_request_when_enabled(
    name: str, event: str, comment: str, expected: bool
) -> None:
    # `fail` is varied separately below; reporting must not consult it at all,
    # which the invariance test pins. Here it is held at the default.
    gate = _gate(name)
    assert (
        evaluate(gate, _contexts(event=event, comment=comment, fail="true")) is expected
    )


@pytest.mark.parametrize("name", _REPORTING_STEPS)
def test_reporting_gate_ignores_the_enforcement_switch(name: str) -> None:
    """Turning enforcement off must not silently stop the reports too."""
    gate = _gate(name)
    with_enforcement = evaluate(
        gate, _contexts(event="pull_request", comment="true", fail="true")
    )
    without_enforcement = evaluate(
        gate, _contexts(event="pull_request", comment="true", fail="false")
    )
    assert with_enforcement is without_enforcement is True


# ── Enforcement is not skippable ─────────────────────────────────────────────


@pytest.mark.parametrize("name", _ENFORCEMENT_STEPS)
@pytest.mark.parametrize("event", _EVENTS)
@pytest.mark.parametrize("comment", ("true", "false"))
def test_enforcement_runs_on_every_event_regardless_of_reporting(
    name: str, event: str, comment: str
) -> None:
    """The regression that would make the required check stop checking.

    A `merge_group` entry must get the same gate the PR got, and the reporting
    switch must not be able to reach it — this is the exact coupling FND-256
    removed.
    """
    gate = _gate(name)
    assert evaluate(gate, _contexts(event=event, comment=comment, fail="true")) is True


@pytest.mark.parametrize("name", _ENFORCEMENT_STEPS)
@pytest.mark.parametrize("event", _EVENTS)
def test_enforcement_is_switched_by_its_own_input(name: str, event: str) -> None:
    gate = _gate(name)
    assert evaluate(gate, _contexts(event=event, comment="true", fail="false")) is False


@pytest.mark.parametrize("name", _ENFORCEMENT_STEPS)
def test_enforcement_gate_never_mentions_the_reporting_input(name: str) -> None:
    """Belt to the invariance test's braces.

    Evaluation proves the coupling is gone for the payloads tried; the textual
    check proves nobody reintroduced the input in a term those payloads happen
    not to distinguish (`|| inputs.add-report-comment-to-pr == 'maybe'`).
    """
    assert "add-report-comment-to-pr" not in _gate(name)


# ── Failure containment ──────────────────────────────────────────────────────


@pytest.mark.parametrize("name", _NON_FATAL_STEPS)
def test_reporting_steps_cannot_fail_the_required_check(name: str) -> None:
    step = _step(name)
    assert step.get("continue-on-error") is True, (
        f"{name!r} must be continue-on-error: a transient failure anywhere in "
        "reporting — a comment post, a pip install, a markdown render — would "
        "otherwise eject the PR from the merge queue"
    )


@pytest.mark.parametrize("name", _ENFORCEMENT_STEPS)
def test_enforcement_steps_are_allowed_to_fail_the_job(name: str) -> None:
    step = _step(name)
    assert "continue-on-error" not in step, (
        f"{name!r} is the check's teeth; continue-on-error would make the "
        "required status check report success on real findings"
    )


# ── Inputs fail closed ───────────────────────────────────────────────────────


def test_enforcement_input_exists_and_defaults_to_on() -> None:
    inputs = _action()["inputs"]
    assert "fail-on-findings" in inputs, (
        "enforcement must have its own input; sharing one with reporting is the "
        "bug FND-256 fixed"
    )
    assert (
        inputs["fail-on-findings"]["default"] == "true"
    ), "a caller that omits the input must still get the security gate"


def test_caller_passes_both_switches_explicitly() -> None:
    """The one caller states both, so editing one is visibly not editing both."""
    workflow = yaml.safe_load(_CALLER.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["trivy"]["steps"]
    invocations = [s for s in steps if s.get("uses") == "./.github/actions/trivy"]
    assert len(invocations) == 1, "expected exactly one trivy action invocation"

    passed = invocations[0].get("with") or {}
    assert passed.get("fail-on-findings") == "true"
    assert passed.get("add-report-comment-to-pr") == "true"
