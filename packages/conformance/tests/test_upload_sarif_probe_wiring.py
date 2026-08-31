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


def _job(source: str) -> dict:  # type: ignore[type-arg]
    """The `upload` job, as GitHub reads it."""
    return yaml.safe_load(source)["jobs"]["upload"]


def _steps(source: str) -> list[dict]:  # type: ignore[type-arg]
    """The `upload` job's steps, as GitHub reads them."""
    return _job(source)["steps"]


def _permissions(source: str) -> dict[str, str]:
    """The scopes in force for the `upload` job.

    A job-level block replaces the workflow-level one outright rather than
    merging with it, so the job's own block wins wherever the key is
    present. Selection is by key presence, not truthiness: `permissions:
    {}` parses to an empty mapping, which GitHub reads as the strongest
    possible replacement — every scope `none` — while `or` would treat it
    as absent and fall back to the workflow block, reporting grants the
    job does not have.
    """
    job = _job(source)
    block = (
        job["permissions"]
        if "permissions" in job
        else yaml.safe_load(source).get("permissions")
    )
    if isinstance(block, dict):
        return dict(block)
    # The scalar shorthands are neither a mapping nor absent. Expanded to
    # what they actually grant for the one scope asserted below, rather
    # than dropped — reporting `read-all` as "no grants" would red a
    # workflow that does have contents access, and a guard that lies in
    # that direction gets deleted rather than fixed. Anything else
    # (`none`, absent) yields no grants, which is what GitHub does too.
    return {"contents": "read"} if block in ("read-all", "write-all") else {}


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


@pytest.mark.parametrize("label,source", _COPIES, ids=lambda v: v)
def test_token_can_read_contents(label: str, source: str) -> None:
    """The checkout needs a grant, not just a step, to reach a private repo.

    A `permissions:` block is exhaustive, not additive: every scope it
    omits is `none` regardless of the repository's
    `default_workflow_permissions`. This workflow declares one for
    `security-events` and `actions`, so omitting `contents` leaves
    `actions/checkout` unable to read the repo — and 77 of the 82 repos
    carrying this file are private, which is the whole population
    FND-1149 is about. The failure would land on the job that must never
    fail, so asserting the checkout step exists is a hollow gate without
    this: the step would be present, correctly configured, and still
    exit non-zero.
    """
    permissions = _permissions(source)
    assert permissions.get("contents") == "read", (
        f"[{label}] the upload job's permissions are {permissions}, which "
        f"leaves `contents` at `none`. `actions/checkout` then cannot read a "
        f"private repo and the probe script never lands, so the workflow "
        f"fails where it is most needed. Add `contents: read`."
    )


# ---------------------------------------------------------------------------
# The resolver behind that assertion
#
# `_permissions` decides which block is in force, and every way of getting
# that wrong makes the guard above pass on a workflow whose token cannot
# read the repo. The empty-mapping case is the sharp one: `permissions: {}`
# is falsy, so selecting the block with `or` falls back to the
# workflow-level grants and reports access the job does not have — while
# GitHub reads it as the strongest possible replacement, every scope
# `none`. These pin the resolver directly rather than through the rendered
# file, which declares only one of these shapes.
# ---------------------------------------------------------------------------

_WORKFLOW_GRANTS = "permissions:\n  contents: read\n  actions: read\n"


def _synthetic(workflow_block: str, job_block: str) -> str:
    """A minimal two-level workflow with the given permissions blocks."""
    return (
        f"on: push\n{workflow_block}jobs:\n"
        f"  upload:\n"
        f"    runs-on: ubuntu-latest\n"
        f"{job_block}"
        f"    steps:\n"
        f"      - run: 'true'\n"
    )


def test_permissions_falls_back_to_workflow_level_when_job_declares_none() -> None:
    """No job block at all: the workflow's grants are the ones in force."""
    source = _synthetic(_WORKFLOW_GRANTS, "")
    assert _permissions(source) == {"contents": "read", "actions": "read"}


def test_permissions_job_block_replaces_rather_than_merges() -> None:
    """A job block is exhaustive: the workflow's other scopes do not survive."""
    source = _synthetic(_WORKFLOW_GRANTS, "    permissions:\n      contents: read\n")
    assert _permissions(source) == {"contents": "read"}


def test_empty_job_block_revokes_everything() -> None:
    """`permissions: {}` is a grant of nothing, not an absent block.

    This is the case a truthiness check gets wrong, and it fails open:
    the resolver would report the workflow-level `contents: read` and
    `test_token_can_read_contents` would stay green on a job whose
    checkout cannot read a private repo.
    """
    source = _synthetic(_WORKFLOW_GRANTS, "    permissions: {}\n")
    assert _permissions(source) == {}
    with pytest.raises(AssertionError, match="leaves `contents` at `none`"):
        test_token_can_read_contents("empty-job-block", source)


@pytest.mark.parametrize("shorthand", ["read-all", "write-all"])
def test_permissions_expands_the_scalar_shorthands(shorthand: str) -> None:
    """`read-all`/`write-all` do grant contents; do not red them."""
    source = _synthetic("", f"    permissions: {shorthand}\n")
    assert _permissions(source)["contents"] == "read"


def test_permissions_treats_scalar_none_as_no_grants() -> None:
    source = _synthetic(_WORKFLOW_GRANTS, "    permissions: none\n")
    assert _permissions(source) == {}
