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
parsed YAML.

Since FND-1994 there is exactly one copy of the body to assert against:
`conformance-upload-sarif-reusable.yaml` in application-sdk. The bootstrap
template and application-sdk's own workflow are both thin callers of it,
so the gate they run is the one asserted here by construction — which is
stronger than the previous arrangement, where the two copies were asserted
separately and could drift between assertions. What the callers are still
checked for is that they *are* callers of that reusable, and that they
carry the two things a reusable cannot: the `workflow_run` trigger and the
`permissions:` grant.

Repos that have not yet been re-synced onto the caller still run their own
inlined copy of the old body. That copy is not asserted here — it is
frozen at whatever the template rendered when they were last bootstrapped,
and the remedy for it is a re-sync, not a test.

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

#: The thin caller, in both places it exists.
_TEMPLATE = "conformance-upload-sarif.yaml"
_CANONICAL = _REPO_ROOT / ".github/workflows" / _TEMPLATE

#: The body both callers delegate to — the file every assertion about the
#: probe gate, the download and the upload runs against.
_REUSABLE_REF = "conformance-upload-sarif-reusable.yaml"
_REUSABLE = _REPO_ROOT / ".github/workflows" / _REUSABLE_REF

#: The suite whose SARIF artifacts these legs collect. Read rather than
#: restated, so a series added to the matrix shows up here as an unuploaded
#: slug instead of as silence.
_SUITE = _REPO_ROOT / ".github/workflows/conformance-reusable.yaml"

#: Series the suite produces SARIF for that application-sdk's caller
#: deliberately does not upload. `security` predates FND-1994 — the inline
#: matrix it replaced did not carry the slug either — so it is recorded as a
#: known gap rather than silently tolerated. Must shrink, never grow.
_NOT_UPLOADED_BY_THE_SDK = {"security"}

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


def _case_id(value: str) -> str:
    """Parametrize id: the label only.

    Both parameters are strings, and the second is a whole workflow file —
    rendered into an id it makes every failure line unreadable, which is how
    a real failure gets skimmed past.
    """
    return "" if "\n" in value else value


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


def _callers() -> list[tuple[str, str]]:
    """`(label, source)` for the template and, if present, the canonical caller.

    The canonical file is absent in an isolated sdist build of this
    package, which is why it is conditional rather than required.
    """
    copies = [("template", render(_TEMPLATE))]
    if _CANONICAL.exists():
        copies.append(("canonical", _CANONICAL.read_text(encoding="utf-8")))
    return copies


_CALLERS = _callers()
_BODIES = (
    [("reusable", _REUSABLE.read_text(encoding="utf-8"))] if _REUSABLE.exists() else []
)


def test_the_body_under_test_exists() -> None:
    """Guard the guard: a missing reusable must not silently empty this file.

    `_BODIES` is built conditionally so that an sdist build of this package
    collects rather than errors at import; a parametrize over an empty list
    silently skips every assertion below, so the absence is asserted here
    instead of being allowed to pass as a green run.
    """
    assert _BODIES, (
        f"{_REUSABLE} is missing, so every gate assertion below is "
        f"parametrized over nothing and cannot fail. If the reusable moved, "
        f"update _REUSABLE."
    )


def test_both_callers_are_under_test() -> None:
    """Guard the guard: a missing canonical file must not silently halve this."""
    assert _CANONICAL.exists(), (
        f"{_CANONICAL} is missing, so every caller assertion below is running "
        f"against the bootstrap template only. application-sdk's own CI runs "
        f"the canonical copy — if it moved, update _CANONICAL."
    )


@pytest.mark.parametrize("label,source", _CALLERS, ids=_case_id)
def test_caller_delegates_to_the_reusable(label: str, source: str) -> None:
    """The caller must call the body these tests assert, not inline its own.

    Without this the file above could be asserted to perfection while the
    callers ran something else entirely — which is the failure mode the
    single-body arrangement exists to remove.
    """
    job = _job(source)
    uses = str(job.get("uses", ""))
    assert _REUSABLE_REF in uses, (
        f"[{label}] the `upload` job's `uses:` is {uses!r}, which is not "
        f"{_REUSABLE_REF}. Every assertion in this file is about that "
        f"reusable's body; a caller running anything else is untested."
    )
    assert "steps" not in job, (
        f"[{label}] the `upload` job declares `steps:` as well as `uses:`, "
        f"which GitHub rejects outright — the caller must be thin."
    )


@pytest.mark.parametrize("label,source", _CALLERS, ids=_case_id)
def test_caller_passes_the_triggering_run_through(label: str, source: str) -> None:
    """A reusable cannot declare `workflow_run`, so the payload is passed in.

    All three inputs are `required: true` on the reusable, so a caller
    missing one fails at startup: zero jobs, no check run, and nothing in
    `gh pr checks` to notice it by.
    """
    with_ = _job(source).get("with", {})
    for key in ("workflow_run_id", "head_branch", "head_sha"):
        value = str(with_.get(key, ""))
        assert "github.event.workflow_run" in value, (
            f"[{label}] the caller passes {key}={value!r}, which does not come "
            f"from the triggering run's payload"
        )


@pytest.mark.parametrize("label,source", _CALLERS, ids=_case_id)
def test_caller_keeps_the_workflow_run_trigger(label: str, source: str) -> None:
    """The trigger is the other thing that cannot move into the reusable.

    `yaml.safe_load` parses the bare `on:` key as the boolean `True`, which
    is why it is read that way rather than by the string.
    """
    triggers = yaml.safe_load(source)[True]
    assert "workflow_run" in triggers, (
        f"[{label}] the caller's triggers are {sorted(triggers)}; without "
        f"`workflow_run` the upload never fires at all."
    )


def _suite_sarif_slugs() -> set[str]:
    """Every `slug` the conformance suite's matrix publishes SARIF for."""
    matrix = yaml.safe_load(_SUITE.read_text(encoding="utf-8"))["jobs"]["suite"][
        "strategy"
    ]["matrix"]["include"]
    return {entry["slug"] for entry in matrix}


def _sdk_caller_slug_entries() -> list[dict]:  # type: ignore[type-arg]
    """The `slugs` input application-sdk's own caller pins, parsed.

    Two loads deep on purpose: the input is a YAML string whose *content* is
    the JSON list, which is exactly the thing nothing parsed before.
    """
    caller = yaml.safe_load(_CANONICAL.read_text(encoding="utf-8"))
    return yaml.safe_load(caller["jobs"]["upload"]["with"]["slugs"])


def test_the_sdk_caller_uploads_every_series_the_suite_produces() -> None:
    """The SDK's own caller pins a `slugs` list, so something must parse it.

    It is the one caller that overrides the reusable's default, and a typo or a
    dropped entry there costs exactly one Security-tab upload — silently, with
    every other assertion in this file still green, because a slug matching no
    artifact is indistinguishable from a series that had no relevant changes.

    Derived from the suite's own matrix rather than restated as ten literals: a
    series added there must be added here too, or named in
    `_NOT_UPLOADED_BY_THE_SDK` on purpose.
    """
    declared = {entry["slug"] for entry in _sdk_caller_slug_entries()}
    produced = _suite_sarif_slugs()

    unknown = declared - produced
    assert not unknown, (
        f"the SDK caller uploads {sorted(unknown)}, which the conformance "
        f"suite publishes no SARIF for — a slug that matches no artifact is "
        f"indistinguishable from a series with no relevant changes, so the leg "
        f"is a permanent no-op nothing reports"
    )

    missing = produced - declared - _NOT_UPLOADED_BY_THE_SDK
    assert not missing, (
        f"the conformance suite publishes SARIF for {sorted(missing)} and the "
        f"SDK caller does not upload it, so those findings never reach the "
        f"Security tab. Add the slug, or record it in "
        f"_NOT_UPLOADED_BY_THE_SDK with the reason."
    )


def test_every_recorded_upload_gap_is_a_series_that_exists() -> None:
    """Guard the exception set: a stale entry there hides a real omission.

    Once `security` is uploaded — or renamed — the entry must go, or it
    silently licenses a gap that is no longer the one it documents.
    """
    stale = _NOT_UPLOADED_BY_THE_SDK - _suite_sarif_slugs()
    assert not stale, (
        f"_NOT_UPLOADED_BY_THE_SDK names {sorted(stale)}, which the suite does "
        f"not produce. Remove the entry."
    )


def test_the_sdk_caller_names_every_leg_it_uploads() -> None:
    """`name:` is the job label; an entry without one renders as a blank check."""
    unnamed = [entry for entry in _sdk_caller_slug_entries() if not entry.get("name")]
    assert not unnamed, f"slug entries without a `name:`: {unnamed}"


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,source", _BODIES, ids=_case_id)
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


@pytest.mark.parametrize("label,source", _BODIES, ids=_case_id)
def test_sarif_download_globs_so_a_retried_upload_is_found(
    label: str, source: str
) -> None:
    """The suite's upload retry publishes `conformance-<slug>-sarif-retry`.

    It has to: a failed `FinalizeArtifact` holds the first attempt's name for
    the rest of the run — invisible to the artifact listing, so `overwrite`
    cannot clear it — and `CreateArtifact` then 409s. An exact `name:` here
    therefore misses every series whose SARIF landed on the second attempt,
    silently, on the same code path FND-1149 was about keeping green.

    `merge-multiple` is what keeps the file at `<slug>.sarif` in the working
    directory rather than under a per-artifact subdirectory, which is where
    the strip step below reads it.
    """
    download = _step_by_id(_steps(source), "download")
    with_ = download.get("with", {})
    pattern = str(with_.get("pattern", ""))
    assert pattern.startswith("conformance-") and pattern.endswith("-sarif*"), (
        f"[{label}] the SARIF download's `pattern:` is {pattern!r}; it must glob "
        f"`conformance-<slug>-sarif*` so the `-retry` artifact is in scope"
    )
    assert "name" not in with_, (
        f"[{label}] the SARIF download still passes `name: {with_.get('name')!r}`, "
        f"which reads only the first attempt's artifact"
    )
    assert with_.get("merge-multiple") is True, (
        f"[{label}] `merge-multiple: true` is missing, so a matched artifact "
        f"lands under its own directory and the strip step's `<slug>.sarif` is "
        f"not there"
    )


@pytest.mark.parametrize("label,source", _BODIES, ids=_case_id)
def test_the_download_cannot_fail_the_workflow(label: str, source: str) -> None:
    """A series with no relevant changes publishes no artifact, and that is fine.

    GitHub Code Scanning marks a tool as "reporting errors" whenever the
    workflow that uploads its SARIF fails, so this one must always exit 0 —
    which is the whole reason it is a separate workflow from the gate.
    """
    download = _step_by_id(_steps(source), "download")
    assert download.get("continue-on-error") is True, (
        f"[{label}] the SARIF download is not `continue-on-error: true`, so a "
        f"series that published nothing reds the workflow that exists to stay "
        f"green"
    )


@pytest.mark.parametrize("label,source", _BODIES, ids=_case_id)
def test_the_default_series_list_covers_a_consumer_app(label: str, source: str) -> None:
    """The four series a connector app's conformance run produces.

    Asserted on the default rather than on a caller, because the callers
    bootstrap writes deliberately do not pin their own list — see
    `test_conformance_upload_sarif_takes_the_default_series_list`. If the
    default is wrong, every consumer repo is wrong at once.
    """
    default = yaml.safe_load(source)[True]["workflow_call"]["inputs"]["slugs"][
        "default"
    ]
    slugs = {entry["slug"] for entry in yaml.safe_load(default)}
    assert {"ci", "error-handling", "prescriptions", "optimizations"} <= slugs, (
        f"[{label}] the default series list is {sorted(slugs)}, which is "
        f"missing a series a consumer app's conformance run produces"
    )


@pytest.mark.parametrize("label,source", _BODIES, ids=_case_id)
def test_the_empty_sarif_gate_reads_the_file_not_the_download_outcome(
    label: str, source: str
) -> None:
    """With `pattern:` a download that matched NOTHING succeeds.

    Confirmed in the pinned action: only the single-artifact `name:` path
    throws when the artifact is absent. So `steps.download.outcome` no longer
    separates "this series published SARIF" from "this series had no relevant
    changes" — it is `success` either way, and a gate reading it would hand
    `upload-sarif` a file that does not exist (or, before that, `jq` a missing
    input) on every skipped series.
    """
    steps = _steps(source)
    gated = [
        step
        for step in steps
        if "codeql-action/upload-sarif" in str(step.get("uses", ""))
        or "jq" in str(step.get("run", ""))
    ]
    assert gated, f"[{label}] neither the strip nor the upload step was found"
    for step in gated:
        condition = str(step.get("if", ""))
        assert "steps.download.outcome" not in condition, (
            f"[{label}] step {step.get('name')!r} gates on "
            f"`steps.download.outcome`, which is `success` even when the "
            f"pattern matched no artifact"
        )
        assert "hashFiles(" in condition, (
            f"[{label}] step {step.get('name')!r} has `if: {condition!r}`, which "
            f"does not test for the SARIF file's presence"
        )


@pytest.mark.parametrize("label,source", _BODIES, ids=_case_id)
def test_probe_invokes_the_vendored_script(label: str, source: str) -> None:
    """The probe runs the script, and the script is still one bootstrap vendors.

    The reusable reads the script out of application-sdk's own checkout, so
    it no longer depends on the consumer's vendored copy. That copy cannot be
    retired yet regardless: every repo still running its own inlined
    pre-FND-1994 body invokes it from its own checkout, and dropping it from
    `MANAGED_ACTION_FILES` would kill their matrix legs with "No such file or
    directory" instead of the clean skip FND-1149 bought.
    """
    probe = _step_by_id(_steps(source), "probe")
    run = str(probe.get("run", ""))
    assert _PROBE_SCRIPT in run, (
        f"[{label}] the probe step does not invoke {_PROBE_SCRIPT}; its `run:` "
        f"is {run!r}"
    )
    assert _PROBE_SCRIPT in dict(MANAGED_ACTION_FILES), (
        f"{_PROBE_SCRIPT} is no longer in MANAGED_ACTION_FILES. Repos still "
        f"running the pre-FND-1994 inlined body invoke it from their own "
        f"checkout, so it must keep being vendored until the fleet has "
        f"migrated onto {_REUSABLE_REF}."
    )


@pytest.mark.parametrize("label,source", _BODIES, ids=_case_id)
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


@pytest.mark.parametrize("label,source", _BODIES, ids=_case_id)
def test_probe_script_is_checked_out_before_it_runs(label: str, source: str) -> None:
    """A `workflow_run` job starts with an empty workspace.

    Without a checkout the probe's `run:` fails on a missing file, which
    — because the step is not `continue-on-error` — reds the very
    workflow this change exists to keep green.

    The checkout must also name `repository:` explicitly. A reusable runs in
    the CALLER's context, so a bare `actions/checkout` there clones the
    consumer repo — which is the right target for the probe's API question
    but the wrong one for the script it asks it with, since a consumer that
    has never been bootstrapped has no copy of it.
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
    repositories = [
        str(step.get("with", {}).get("repository", "")) for step in checkouts
    ]
    assert any(repo == "atlanhq/application-sdk" for repo in repositories), (
        f"[{label}] the checkout before the probe targets {repositories}, not "
        f"atlanhq/application-sdk. Running in the caller's context, that "
        f"clones the consumer repo — where {_PROBE_SCRIPT} is present only if "
        f"bootstrap has ever run there."
    )


@pytest.mark.parametrize("label,source", _CALLERS, ids=_case_id)
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
