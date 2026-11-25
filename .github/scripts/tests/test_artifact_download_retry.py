"""Cross-file guard: artifact downloads go through `download-artifact-retry`.

The sibling guard for uploads covers the artifact service failing to *finalize*.
This one covers the other end of the same wire: `download-artifact` asking the
service for the run's artifacts and getting

    Failed to ListArtifacts: Received non-retryable error:
    Failed request: (403) Forbidden: Error from intermediary

The bytes are intact and the artifact is listed in the run; the step even
prints its id, size and digest immediately before failing. `download-artifact`
classifies the 403 as non-retryable and gives up on the first occurrence, so a
gating job dies on a flake that a second attempt absorbs. Observed on an app
repo's Security Gate, a required check, where it stalled an otherwise
auto-mergeable PR until someone re-ran the job by hand.

The retry lives in one place, `.github/actions/download-artifact-retry`. This
guard pins its rules once, then checks that nothing in .github/ calls
`actions/download-artifact` directly except the composite and `EXEMPT`
downloads, each with the reason it needs no retry.

Where this differs from the upload guard: a retried *upload* must take a
DISTINCT name, because a failed finalize leaves an invisible record holding the
name for the rest of the run. A failed read burns nothing, so the download
retry reuses the first attempt's selector. The two rules look contradictory
side by side and are not; they are about opposite ends of the same call.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOWNLOAD_ACTION = "actions/download-artifact@"
COMPOSITE = "actions/download-artifact-retry/action.yaml"
COMPOSITE_REF = ".github/actions/download-artifact-retry"
LOCAL_COMPOSITE_REF = f"./{COMPOSITE_REF}"

# `steps.<id>.outcome == 'failure'`: the only shape that makes a step a retry.
OUTCOME_GUARD_RE = re.compile(r"steps\.([A-Za-z0-9_-]+)\.outcome\s*==\s*'failure'")

# Downloads that do NOT need retry hardening, each with the reason it is exempt.
# Keyed by (path suffix, artifact selector) so an exemption cannot silently
# widen to other downloads added to the same file later.
EXEMPT = {
    (
        "workflows/tests-reusable.yaml",
        "sdr-integration-tests-${{ inputs.app-name }}*-results*",
    ): "evidence for a report, not a gate; the job already tolerates a miss",
    (
        "workflows/tests-reusable.yaml",
        "unit-test-coverage*",
    ): "scorecard evidence; a miss degrades the report and gates nothing",
    (
        "workflows/tests-reusable.yaml",
        "integration-test-results*",
    ): "scorecard evidence; a miss degrades the report and gates nothing",
    (
        "workflows/conformance-upload-sarif-reusable.yaml",
        "conformance-${{ matrix.slug }}-sarif*",
    ): "workflow_run consumer; nothing gates on it and there is no queue entry to eject",
}

Step = dict
Scope = tuple[str, str, list[Step]]


def _docs() -> dict[str, dict]:
    files = sorted((ROOT / "workflows").glob("*.y*ml"))
    files += sorted((ROOT / "actions").glob("*/action.y*ml"))
    docs = {}
    for path in files:
        doc = yaml.safe_load(path.read_text())
        if isinstance(doc, dict):
            docs[str(path.relative_to(ROOT))] = doc
    return docs


def _scopes_from_doc(rel: str, doc: dict) -> list[Scope]:
    """(file, scope, steps) per job and per composite `runs` block."""
    scopes: list[Scope] = []
    for job_id, job in (doc.get("jobs") or {}).items():
        if isinstance(job, dict):
            steps = [s for s in (job.get("steps") or []) if isinstance(s, dict)]
            if steps:
                scopes.append((rel, f"job {job_id}", steps))
    runs = doc.get("runs") or {}
    if isinstance(runs, dict):
        steps = [s for s in (runs.get("steps") or []) if isinstance(s, dict)]
        if steps:
            scopes.append((rel, "composite runs", steps))
    return scopes


def _scopes() -> list[Scope]:
    return [s for rel, doc in _docs().items() for s in _scopes_from_doc(rel, doc)]


def _is_direct_download(step: Step) -> bool:
    return DOWNLOAD_ACTION in str(step.get("uses", ""))


def _is_composite_call(step: Step) -> bool:
    return str(step.get("uses", "")).split("@")[0].endswith(COMPOSITE_REF)


def _with(step: Step) -> dict:
    value = step.get("with")
    return value if isinstance(value, dict) else {}


def _selector(step: Step) -> str:
    """What the step asks the service for: a `pattern` glob or a bare `name`."""
    inputs = _with(step)
    return str(inputs.get("pattern") or inputs.get("name") or "")


def _label(rel: str, scope: str, step: Step) -> str:
    return f"{rel} [{scope}] '{step.get('name') or step.get('uses')}'"


def _guards_on(step: Step, step_id: str) -> bool:
    return step_id in OUTCOME_GUARD_RE.findall(str(step.get("if", "")))


def _composite_calls() -> list[tuple[str, str, Step]]:
    return [
        (rel, scope, step)
        for rel, scope, steps in _scopes()
        for step in steps
        if _is_composite_call(step)
    ]


def test_the_composite_retries_the_same_selector_after_a_wait():
    doc = yaml.safe_load((ROOT / COMPOSITE).read_text())
    first, wait, retry = doc["runs"]["steps"]

    assert _is_direct_download(first) and first.get("id")
    assert first.get("continue-on-error") is True, (
        "the first attempt must not fail the step before the retry runs"
    )
    assert str(first.get("if")) == "always()", (
        "the caller's `if:` is the only gate: an `if:` here would skip the "
        "download whenever an earlier step of the caller's job failed"
    )

    assert "sleep 20" in str(wait.get("run")), "the retry needs a backoff"
    assert _guards_on(wait, first["id"]) and "always()" in str(wait.get("if"))

    assert _is_direct_download(retry)
    assert _guards_on(retry, first["id"]) and "always()" in str(retry.get("if"))
    assert retry.get("continue-on-error") is not True, (
        "losing both attempts must fail the step; a caller that tolerates it "
        "sets continue-on-error on its own step"
    )
    for step in (first, retry):
        inputs = _with(step)
        assert inputs.get("pattern") == "${{ inputs.pattern }}", (
            "by pattern, never `name:`: an upload retry lands as `<name>-retry`"
        )
        assert inputs.get("merge-multiple") is True
        assert inputs.get("path") == "${{ inputs.path }}"


def test_the_scan_finds_composite_calls():
    """A scan that silently matched nothing would pass every assertion below."""
    assert len(_scopes()) > 50, "scope discovery collapsed"
    assert len(_composite_calls()) >= 6, "composite call discovery collapsed"


def test_no_direct_download_outside_the_composite():
    problems = []
    for rel, scope, steps in _scopes():
        if rel == COMPOSITE:
            continue
        for step in steps:
            if _is_direct_download(step) and (rel, _selector(step)) not in EXEMPT:
                problems.append(_label(rel, scope, step))
    assert not problems, (
        f"Use `atlanhq/application-sdk/{COMPOSITE_REF}@main` instead of "
        f"`actions/download-artifact`, or add an EXEMPT entry with the reason the "
        "download needs no retry:\n  " + "\n  ".join(problems)
    )


def test_every_call_globs_so_it_finds_a_retried_upload():
    """An upload retried through `upload-artifact-retry` is `<name>-retry`."""
    bad = [
        f"{_label(rel, scope, step)}: pattern {_selector(step)!r}"
        for rel, scope, step in _composite_calls()
        if not _selector(step).endswith("*")
    ]
    assert not bad, "Pass `<name>*`, not the bare name:\n  " + "\n  ".join(bad)


def test_a_relative_ref_is_only_used_where_this_repo_is_the_workspace():
    """`./.github/actions/...` resolves against the caller's workspace. In a
    reusable workflow or a composite that is a consumer repo, which has no copy
    of the composite."""
    problems = []
    for rel, doc in _docs().items():
        on = doc.get("on", doc.get(True))
        reusable = rel.startswith("actions/") or (
            isinstance(on, dict) and "workflow_call" in on
        )
        if not reusable:
            continue
        for _, scope, steps in _scopes_from_doc(rel, doc):
            for step in steps:
                if str(step.get("uses", "")).startswith(LOCAL_COMPOSITE_REF):
                    problems.append(_label(rel, scope, step))
    assert not problems, (
        f"Reusable workflows and composites must call `atlanhq/application-sdk/"
        f"{COMPOSITE_REF}@main`:\n  " + "\n  ".join(problems)
    )


def test_exemptions_still_point_at_real_downloads():
    """A stale exemption would silently excuse a file that no longer downloads."""
    live = {
        (rel, _selector(step))
        for rel, _, steps in _scopes()
        for step in steps
        if _is_direct_download(step)
    }
    stale = sorted(set(EXEMPT) - live)
    assert not stale, f"EXEMPT entries no longer match any download step: {stale}"
