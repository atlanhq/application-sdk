"""Cross-file guard: artifact uploads on gating paths must retry.

`actions/upload-artifact` treats the artifact service's finalize 403 as
non-retryable and fails the step on the first occurrence, even though the bytes
already landed. On a gating path that fails the job, fails the aggregate gate,
and ejects the merge-queue entry — a full CI run wasted on a flake that a second
attempt would have absorbed.

This guard reads the workflow and composite-action YAML directly rather than
trusting a checked-in list, so a newly added upload step fails here instead of
silently reintroducing the flake. It is the same shape as the other cross-file
guards in this suite that assert a property of the YAML outside .github/scripts/.

(Deliberately avoids naming those sibling guards' scripts: this suite discovers
call sites by grepping .github/ for a script's name, and a prose mention here
would register this file as one of its callers.)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
UPLOAD_ACTION = "actions/upload-artifact"

# Uploads that do NOT need retry hardening, each with the reason it is exempt.
# Keyed by (path suffix, artifact name) so an exemption cannot silently widen to
# other uploads added to the same file later.
EXEMPT = {
    (
        "workflows/daily-security-scan.yml",
        "security-scan-raw-results",
    ): "scheduled scan; nothing gates on it and there is no merge queue to eject",
    (
        "workflows/v3-readiness-check.yaml",
        "v3-readiness-report",
    ): "manually dispatched report; not on a PR or merge_group path",
}

# Files that are not valid YAML today, so their steps cannot be inspected.
# This is a quarantine list, not an allowance: the test below asserts it does not
# grow, and each entry is a bug to fix rather than a shape to copy.
#
# scheduled-trivy-scan.yml: the `read -r -d '' DESCRIPTION << DESC_EOF` heredoc
# body sits at column 0, which terminates the enclosing `run: |` block scalar, so
# the markdown after it parses as YAML (`**Service:**` reads as an alias). GitHub
# cannot parse it either — it lists the workflow by path instead of by `name:`
# and every run fails. It has no upload-artifact step, so nothing is skipped
# here by quarantining it.
UNPARSEABLE = {"workflows/scheduled-trivy-scan.yml"}


def _yaml_files() -> list[Path]:
    files = sorted((ROOT / "workflows").glob("*.y*ml"))
    files += sorted((ROOT / "actions").glob("*/action.y*ml"))
    return files


def _steps(doc: dict) -> list[dict]:
    """Every step in a workflow's jobs or a composite action's runs block."""
    steps: list[dict] = []
    for job in (doc.get("jobs") or {}).values():
        if isinstance(job, dict):
            steps.extend(s for s in (job.get("steps") or []) if isinstance(s, dict))
    runs = doc.get("runs") or {}
    if isinstance(runs, dict):
        steps.extend(s for s in (runs.get("steps") or []) if isinstance(s, dict))
    return steps


def _unparseable() -> set[str]:
    broken = set()
    for path in _yaml_files():
        try:
            yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            broken.add(_rel(path))
    return broken


def _uploads() -> list[tuple[Path, dict, list[dict]]]:
    found = []
    for path in _yaml_files():
        if _rel(path) in UNPARSEABLE:
            continue
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            pytest.fail(
                f"{_rel(path)} is not valid YAML ({exc.__class__.__name__}) and is "
                f"not in UNPARSEABLE: {exc}"
            )
        if not isinstance(doc, dict):
            continue
        steps = _steps(doc)
        for step in steps:
            if UPLOAD_ACTION in str(step.get("uses", "")):
                found.append((path, step, steps))
    return found


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _artifact_name(step: dict) -> str:
    return str((step.get("with") or {}).get("name", ""))


def _is_retry(step: dict) -> bool:
    return "(retry)" in str(step.get("name", ""))


def test_no_new_unparseable_workflow_yaml():
    """Quarantine must shrink, never grow. An unparseable workflow is invisible
    to every guard in this suite — and GitHub cannot run it either."""
    broken = _unparseable()
    assert broken <= UNPARSEABLE, (
        "New unparseable workflow/action YAML (GitHub will fail these runs and no "
        f"guard can inspect them): {sorted(broken - UNPARSEABLE)}"
    )
    fixed = sorted(UNPARSEABLE - broken)
    assert not fixed, (
        f"{fixed} now parses — remove it from UNPARSEABLE so its steps start "
        f"being guarded."
    )


def test_the_guard_actually_finds_uploads():
    """A scan that silently matches nothing would pass every assertion below."""
    assert len(_uploads()) >= 10


def test_every_gating_upload_has_a_retry_attempt():
    missing = []
    for path, step, steps in _uploads():
        if _is_retry(step):
            continue
        key = (_rel(path), _artifact_name(step))
        if key in EXEMPT:
            continue
        step_id = step.get("id")
        if not step_id:
            missing.append(
                f"{_rel(path)}: '{step.get('name')}' has no `id` to retry on"
            )
            continue
        guard = f"steps.{step_id}.outcome == 'failure'"
        if not any(guard in str(other.get("if", "")) for other in steps):
            missing.append(
                f"{_rel(path)}: '{step.get('name')}' has no retry step guarded on {guard}"
            )
    assert not missing, (
        "Artifact uploads on gating paths must be paired with a retry attempt "
        "(see .github/actions or any conformance-*.yaml for the pattern), or "
        "added to EXEMPT in this test with a reason:\n  " + "\n  ".join(missing)
    )


def test_first_attempt_never_fails_the_job_before_the_retry_runs():
    """Without continue-on-error the job dies and the retry never executes."""
    bad = []
    for path, step, _ in _uploads():
        if _is_retry(step):
            continue
        if (_rel(path), _artifact_name(step)) in EXEMPT:
            continue
        if step.get("continue-on-error") is not True:
            bad.append(f"{_rel(path)}: '{step.get('name')}'")
    assert not bad, (
        "The first upload attempt must set `continue-on-error: true`, otherwise "
        "the job fails before its retry can run:\n  " + "\n  ".join(bad)
    )


def test_retries_overwrite_so_a_partial_artifact_cannot_block_them():
    """A failed finalize can leave a same-named artifact behind; without
    overwrite the retry 409s on it and the hardening is worthless."""
    bad = []
    for path, step, _ in _uploads():
        if not _is_retry(step):
            continue
        if (step.get("with") or {}).get("overwrite") is not True:
            bad.append(f"{_rel(path)}: '{step.get('name')}'")
    assert not bad, "Retry attempts must set `overwrite: true`:\n  " + "\n  ".join(bad)


def test_exemptions_still_point_at_real_uploads():
    """A stale exemption would silently excuse a file that no longer uploads."""
    live = {(_rel(p), _artifact_name(s)) for p, s, _ in _uploads() if not _is_retry(s)}
    stale = sorted(set(EXEMPT) - live)
    assert not stale, f"EXEMPT entries no longer match any upload step: {stale}"
