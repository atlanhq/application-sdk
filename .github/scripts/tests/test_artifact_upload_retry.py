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

What counts as a retry
----------------------
Structure, never the step's name. An earlier version of this guard classified any
upload step whose name contained "(retry)" as a retry and exempted it from every
assertion — so a *first* attempt that happened to carry that substring vanished
from validation, and the pairing check only asked whether *some* step in the file
had an outcome guard. Both let a genuinely unretried upload keep the guard green.

Here a retry is an `actions/upload-artifact` step whose `if:` is guarded on
`steps.<id>.outcome == 'failure'` where `<id>` is another upload step **in the
same job or composite**, and which uploads the same artifact name and path. That
is checked, not assumed: a companion step that guards on the right outcome but
uploads something else, or is not an upload at all, does not satisfy the pairing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
UPLOAD_ACTION = "actions/upload-artifact"

# `steps.<id>.outcome == 'failure'` — the only shape that makes a step a retry.
OUTCOME_GUARD_RE = re.compile(r"steps\.([A-Za-z0-9_-]+)\.outcome\s*==\s*'failure'")

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

# Files that are not valid YAML, so their steps cannot be inspected. Empty, and
# meant to stay that way: an unparseable workflow is invisible to every guard in
# this suite, and GitHub cannot run it either. Kept as a mechanism rather than
# deleted because `test_no_new_unparseable_workflow_yaml` asserts in both
# directions — nothing new may land here, and anything listed that starts parsing
# (or is removed from the repo) must be taken off the list.
#
# Its one occupant, scheduled-trivy-scan.yml, was deleted rather than repaired:
# it had never parsed since being added, so its scan had never run once, and
# `daily-security-scan.yml` already covers the same image + deps scan hourly with
# CVE dedup. Repairing the YAML would have activated a duplicate ticket-filing
# scan that has no dedup of its own.
UNPARSEABLE: set[str] = set()


def _yaml_files() -> list[Path]:
    files = sorted((ROOT / "workflows").glob("*.y*ml"))
    files += sorted((ROOT / "actions").glob("*/action.y*ml"))
    return files


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _unparseable() -> set[str]:
    broken = set()
    for path in _yaml_files():
        try:
            yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            broken.add(_rel(path))
    return broken


def _scopes_from_doc(rel: str, doc: dict) -> list[tuple[str, str, list[dict]]]:
    """(file, scope, steps) per job and per composite `runs` block.

    Scoping matters: `steps.<id>` only resolves within one job, so a retry in a
    *different* job of the same file must not satisfy another job's pairing.
    """
    scopes: list[tuple[str, str, list[dict]]] = []
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


def _scopes() -> list[tuple[str, str, list[dict]]]:
    scopes: list[tuple[str, str, list[dict]]] = []
    for path in _yaml_files():
        rel = _rel(path)
        if rel in UNPARSEABLE:
            continue
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            pytest.fail(
                f"{rel} is not valid YAML ({exc.__class__.__name__}) and is not "
                f"in UNPARSEABLE: {exc}"
            )
        if not isinstance(doc, dict):
            continue
        scopes.extend(_scopes_from_doc(rel, doc))
    return scopes


def _is_upload(step: dict) -> bool:
    return UPLOAD_ACTION in str(step.get("uses", ""))


def _with(step: dict) -> dict:
    value = step.get("with")
    return value if isinstance(value, dict) else {}


def _artifact_name(step: dict) -> str:
    return str(_with(step).get("name", ""))


def _artifact_path(step: dict) -> str:
    """Normalised so a multi-line `path:` block compares equal across steps."""
    raw = str(_with(step).get("path", ""))
    return "\n".join(line.strip() for line in raw.splitlines() if line.strip())


def _retry_target(step: dict) -> str | None:
    """The step id this upload claims to retry, or None if it guards on nothing."""
    match = OUTCOME_GUARD_RE.search(str(step.get("if", "")))
    return match.group(1) if match else None


def _classify(steps: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Split a scope's uploads into (first_attempts, retries_by_target_id).

    A retry must guard on the outcome of another *upload* step in this same
    scope. An upload guarded on some non-upload step's outcome is treated as a
    first attempt, so it still has to prove it has a retry of its own.
    """
    uploads = [s for s in steps if _is_upload(s)]
    upload_ids = {s.get("id") for s in uploads if s.get("id")}

    retries: dict[str, list[dict]] = {}
    first_attempts: list[dict] = []
    for step in uploads:
        target = _retry_target(step)
        if target and target in upload_ids:
            retries.setdefault(target, []).append(step)
        else:
            first_attempts.append(step)
    return first_attempts, retries


def _live_first_attempts() -> list[tuple[str, str, dict, dict[str, list[dict]]]]:
    """Every non-exempt first-attempt upload, with its scope's retry index."""
    live = []
    for rel, scope, steps in _scopes():
        first_attempts, retries = _classify(steps)
        for step in first_attempts:
            if (rel, _artifact_name(step)) in EXEMPT:
                continue
            live.append((rel, scope, step, retries))
    return live


def _label(rel: str, scope: str, step: dict) -> str:
    return f"{rel} [{scope}] '{step.get('name') or step.get('uses')}'"


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


def test_the_guard_actually_finds_uploads_and_retries():
    """A scan that silently matched nothing would pass every assertion below.

    Asserts on both halves: if the retry-classification broke, `retries` would
    empty out while `first_attempts` doubled, and the pairing test would then be
    asserting over the wrong set.
    """
    scopes = _scopes()
    assert len(scopes) > 50, "scope discovery collapsed"

    total_first, total_retries = 0, 0
    for _, _, steps in scopes:
        first_attempts, retries = _classify(steps)
        total_first += len(first_attempts)
        total_retries += sum(len(v) for v in retries.values())
    assert total_first >= 10, f"only {total_first} first-attempt uploads found"
    assert total_retries >= 10, f"only {total_retries} retry uploads found"


def test_every_gating_upload_has_a_matching_retry_upload():
    """The retry must be a real upload of the same artifact, in the same scope."""
    problems = []
    for rel, scope, step, retries in _live_first_attempts():
        where = _label(rel, scope, step)
        step_id = step.get("id")
        if not step_id:
            problems.append(f"{where}: no `id`, so nothing can guard a retry on it")
            continue

        companions = retries.get(step_id, [])
        if not companions:
            problems.append(
                f"{where}: no `{UPLOAD_ACTION}` step in this scope is guarded on "
                f"steps.{step_id}.outcome == 'failure'"
            )
            continue

        # A companion that uploads a different artifact is not a retry of this one.
        for companion in companions:
            if _artifact_name(companion) != _artifact_name(step):
                problems.append(
                    f"{where}: its retry uploads artifact "
                    f"'{_artifact_name(companion)}', not '{_artifact_name(step)}'"
                )
            if _artifact_path(companion) != _artifact_path(step):
                problems.append(
                    f"{where}: its retry uploads a different path "
                    f"({_artifact_path(companion)!r} != {_artifact_path(step)!r})"
                )
            if companion is step:
                problems.append(f"{where}: a step cannot be its own retry")

    assert not problems, (
        "Every artifact upload on a gating path needs a companion "
        f"`{UPLOAD_ACTION}` step, in the same job/composite, guarded on the first "
        "attempt's outcome and uploading the same name and path (see any "
        "conformance-*.yaml for the pattern) — or an EXEMPT entry with a "
        "reason:\n  " + "\n  ".join(problems)
    )


def test_first_attempt_never_fails_the_job_before_the_retry_runs():
    """Without continue-on-error the job dies and the retry never executes."""
    bad = [
        _label(rel, scope, step)
        for rel, scope, step, _ in _live_first_attempts()
        if step.get("continue-on-error") is not True
    ]
    assert not bad, (
        "The first upload attempt must set `continue-on-error: true`, otherwise "
        "the job fails before its retry can run:\n  " + "\n  ".join(bad)
    )


def test_retries_overwrite_so_a_partial_artifact_cannot_block_them():
    """A failed finalize can leave a same-named artifact behind; without
    overwrite the retry 409s on it and the hardening is worthless."""
    bad = []
    for rel, scope, steps in _scopes():
        _, retries = _classify(steps)
        for companions in retries.values():
            for companion in companions:
                if _with(companion).get("overwrite") is not True:
                    bad.append(_label(rel, scope, companion))
    assert not bad, "Retry attempts must set `overwrite: true`:\n  " + "\n  ".join(bad)


def test_a_retry_is_not_identified_by_its_name():
    """Regression: naming a step '(retry)' must not exempt it from validation.

    Synthesised rather than read off disk, because the point is what the
    classifier does with a shape the repo should never contain.
    """
    steps = [
        {
            "name": "Upload something (retry)",
            "uses": f"{UPLOAD_ACTION}@v7",
            "with": {"name": "thing", "path": "thing/"},
        }
    ]
    first_attempts, retries = _classify(steps)
    assert not retries, "a name substring must not make a step a retry"
    assert len(first_attempts) == 1, (
        "a step named '(retry)' with no outcome guard is an unretried first "
        "attempt and must still be validated"
    )


def test_a_companion_guarded_on_a_non_upload_step_is_not_a_retry():
    """Regression: the guard must require the retry to be an upload of its own."""
    steps = [
        {
            "id": "build",
            "name": "Build",
            "run": "make",
        },
        {
            "id": "upload",
            "name": "Upload",
            "continue-on-error": True,
            "uses": f"{UPLOAD_ACTION}@v7",
            "with": {"name": "thing", "path": "thing/"},
        },
        {
            # Guards on the right outcome but is not an upload at all.
            "name": "Tell someone it failed",
            "if": "steps.upload.outcome == 'failure'",
            "run": "echo oh no",
        },
    ]
    first_attempts, retries = _classify(steps)
    assert [s["id"] for s in first_attempts] == ["upload"]
    assert not retries, "a non-upload step must not count as the retry"


def test_a_retry_in_another_job_does_not_satisfy_the_pairing():
    """`steps.<id>` only resolves within one job, so a retry in a sibling job is
    not a retry at all — it would reference an id that does not exist there."""
    doc = {
        "jobs": {
            "builder": {
                "steps": [
                    {
                        "id": "upload",
                        "name": "Upload",
                        "continue-on-error": True,
                        "uses": f"{UPLOAD_ACTION}@v7",
                        "with": {"name": "thing", "path": "thing/"},
                    }
                ]
            },
            "elsewhere": {
                "steps": [
                    {
                        "name": "Upload (retry)",
                        "if": "steps.upload.outcome == 'failure'",
                        "uses": f"{UPLOAD_ACTION}@v7",
                        "with": {"name": "thing", "path": "thing/", "overwrite": True},
                    }
                ]
            },
        }
    }
    scopes = _scopes_from_doc("synthetic.yaml", doc)
    assert len(scopes) == 2, "jobs must be separate scopes"

    by_scope = {scope: _classify(steps) for _, scope, steps in scopes}
    # The uploading job has a first attempt and no retry of its own...
    builder_first, builder_retries = by_scope["job builder"]
    assert [s["id"] for s in builder_first] == ["upload"]
    assert not builder_retries
    # ...and the sibling job's step is itself an unpaired first attempt, because
    # the id it guards on does not exist in its scope.
    other_first, other_retries = by_scope["job elsewhere"]
    assert len(other_first) == 1
    assert not other_retries


def test_exemptions_still_point_at_real_uploads():
    """A stale exemption would silently excuse a file that no longer uploads."""
    live = set()
    for rel, _, steps in _scopes():
        first_attempts, _ = _classify(steps)
        live.update((rel, _artifact_name(s)) for s in first_attempts)
    stale = sorted(set(EXEMPT) - live)
    assert not stale, f"EXEMPT entries no longer match any upload step: {stale}"
