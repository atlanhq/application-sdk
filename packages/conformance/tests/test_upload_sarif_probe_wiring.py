"""The SARIF upload's eligibility gate must be wired, not just mentioned.

FND-1149: `conformance-upload-sarif.yaml` failed on every push to main in
the 77 private repos of the 82 carrying it, because
`github/codeql-action/upload-sarif` fails the whole job where code
scanning is unavailable. The fix gates the upload on a probe of the
repo's actual eligibility.

`test_bootstrap.py` covers this workflow with string-contains assertions
(trigger, `continue-on-error`, ref/sha, permissions, series slugs). That
shape cannot see the gate: `"steps.probe" in content` passes just as
happily when the probe's output is never read, when the `if:` names a
step id that does not exist, or when the script the `run:` invokes is not
one bootstrap vendors. So the gate is asserted here structurally, off the
parsed YAML — and against BOTH copies, since bootstrap force-writes the
template into every consumer repo while application-sdk's own CI runs the
canonical file.

The `run:`-is-straight-line assertion is the other half. The probe began
life as inlined `if`/`else` shell, which docs/standards/ci.md forbids
precisely because those branches cannot be regression-tested — and
`set -uo pipefail` (no `-e`) meant a failed `gh api` silently resolved to
"ineligible" with the job still green. Keeping the branching in
`.github/scripts/probe_code_scanning.py` is what lets
`.github/scripts/tests/test_probe_code_scanning.py` exercise the four
cases; this test stops it drifting back into YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conformance.bootstrap.render import MANAGED_ACTION_FILES, render

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The workflow under test, in both places it exists.
_TEMPLATE = "conformance-upload-sarif.yaml"
_CANONICAL = _REPO_ROOT / ".github/workflows" / _TEMPLATE

#: The vendored script the probe step must invoke. Asserted against
#: MANAGED_ACTION_FILES below rather than restated, so renaming the script
#: without re-registering it fails here instead of at 3am in 82 repos.
_PROBE_SCRIPT = ".github/scripts/probe_code_scanning.py"

#: Shell keywords docs/standards/ci.md keeps out of inlined `run:` blocks.
#: Closers (`then`, `fi`, `esac`, `done`) are listed too, so a branch is
#: caught from either end. Matched as whole shell words rather than as
#: substrings — `fi` and `if` appear inside ordinary filenames, and a guard
#: that reds on a rename teaches people to delete the guard.
_BRANCHING_KEYWORDS = frozenset(
    {
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "case",
        "esac",
        "for",
        "while",
        "until",
        "do",
        "done",
    }
)


def _steps(source: str) -> list[dict]:  # type: ignore[type-arg]
    """The `upload` job's steps, as GitHub reads them."""
    workflow = yaml.safe_load(source)
    return workflow["jobs"]["upload"]["steps"]


def _step_by_id(steps: list[dict], step_id: str) -> dict:  # type: ignore[type-arg]
    matches = [step for step in steps if step.get("id") == step_id]
    assert len(matches) == 1, (
        f"expected exactly one step with `id: {step_id}`, found {len(matches)}. "
        f"The upload step's `if:` reads `steps.{step_id}.outputs`, which "
        f"evaluates to the empty string — not an error — when the id is absent."
    )
    return matches[0]


def _both_copies() -> list[tuple[str, str]]:
    """`(label, source)` for the template and, if present, the canonical file.

    The canonical file is absent in an isolated sdist build of this
    package, which is why it is conditional rather than required.
    """
    copies = [("template", render(_TEMPLATE))]
    if _CANONICAL.exists():
        copies.append(("canonical", _CANONICAL.read_text(encoding="utf-8")))
    return copies


_COPIES = _both_copies()


def test_both_copies_are_under_test() -> None:
    """Guard the guard: a missing canonical file must not silently halve this."""
    assert _CANONICAL.exists(), (
        f"{_CANONICAL} is missing, so every assertion below is running against "
        f"the bootstrap template only. application-sdk's own CI runs the "
        f"canonical copy — if it moved, update _CANONICAL."
    )


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,source", _COPIES, ids=lambda v: v)
def test_upload_is_gated_on_the_probe_output(label: str, source: str) -> None:
    """`upload-sarif` runs only when the probe resolved the repo eligible."""
    steps = _steps(source)
    _step_by_id(steps, "probe")  # the id the condition below depends on
    uploads = [
        step
        for step in steps
        if "github/codeql-action/upload-sarif" in str(step.get("uses", ""))
    ]
    assert uploads, f"[{label}] no upload-sarif step — the gate has nothing to gate"
    for step in uploads:
        condition = str(step.get("if", ""))
        assert "steps.probe.outputs.available == 'true'" in condition, (
            f"[{label}] the upload-sarif step's `if:` is {condition!r}, which "
            f"does not require the probe's verdict. Without it the step runs "
            f"on private repos with no GHAS licence and fails the job — which "
            f"is exactly FND-1149."
        )


@pytest.mark.parametrize("label,source", _COPIES, ids=lambda v: v)
def test_probe_invokes_the_vendored_script(label: str, source: str) -> None:
    """The probe runs the script, and the script is one bootstrap vendors."""
    probe = _step_by_id(_steps(source), "probe")
    run = str(probe.get("run", ""))
    assert _PROBE_SCRIPT in run, (
        f"[{label}] the probe step does not invoke {_PROBE_SCRIPT}; its `run:` "
        f"is {run!r}"
    )
    assert _PROBE_SCRIPT in dict(MANAGED_ACTION_FILES), (
        f"{_PROBE_SCRIPT} is invoked by {_TEMPLATE} but is not in "
        f"MANAGED_ACTION_FILES, so bootstrap never writes it into a consumer "
        f"repo and every matrix leg there dies with 'No such file or "
        f"directory' instead of skipping cleanly."
    )


@pytest.mark.parametrize("label,source", _COPIES, ids=lambda v: v)
def test_probe_step_run_is_straight_line(label: str, source: str) -> None:
    """No conditional logic in the probe's inlined shell (docs/standards/ci.md)."""
    probe = _step_by_id(_steps(source), "probe")
    lines = [line.strip() for line in str(probe.get("run", "")).splitlines()]
    body = [line for line in lines if line and not line.startswith("#")]
    assert len(body) == 1, (
        f"[{label}] the probe's `run:` has {len(body)} statements: {body}. It "
        f"must be a single invoke — the decision belongs in "
        f"{_PROBE_SCRIPT}, where a pytest can reach it."
    )
    words = {word.strip(";&|()") for word in body[0].split()}
    offenders = sorted(words & _BRANCHING_KEYWORDS)
    assert not offenders, (
        f"[{label}] the probe's `run:` reintroduces shell branching "
        f"({offenders}). Those branches cannot be regression-tested, and "
        f"bootstrap force-writes this file into every consumer repo."
    )


@pytest.mark.parametrize("label,source", _COPIES, ids=lambda v: v)
def test_probe_script_is_checked_out_before_it_runs(label: str, source: str) -> None:
    """A `workflow_run` job starts with an empty workspace.

    Without a checkout the probe's `run:` fails on a missing file, which
    — because the step is not `continue-on-error` — reds the very
    workflow this change exists to keep green.
    """
    steps = _steps(source)
    probe_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "probe"
    )
    checkouts = [
        step
        for step in steps[:probe_index]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts, (
        f"[{label}] nothing checks the repo out before the probe step, so "
        f"{_PROBE_SCRIPT} does not exist when the `run:` fires."
    )
    patterns = [
        str(step.get("with", {}).get("sparse-checkout", "")) for step in checkouts
    ]
    assert any(not pattern or _PROBE_SCRIPT in pattern for pattern in patterns), (
        f"[{label}] the checkout before the probe is sparse and its patterns "
        f"{patterns} exclude {_PROBE_SCRIPT}, so the file is still absent."
    )
