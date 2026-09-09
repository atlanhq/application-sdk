"""Cross-file guard: artifact downloads on gating paths must retry.

The sibling guard for uploads covers the artifact service failing to *finalize*.
This one covers the other end of the same wire: `download-artifact` asking the
service for the run's artifacts and getting

    Failed to ListArtifacts: Received non-retryable error:
    Failed request: (403) Forbidden: Error from intermediary

The bytes are intact and the artifact is listed in the run — the step even
prints its id, size and digest immediately before failing. `download-artifact`
classifies the 403 as non-retryable and gives up on the first occurrence, so a
gating job dies on a flake that a second attempt absorbs. Observed on an app
repo's Security Gate, a required check, where it stalled an otherwise
auto-mergeable PR until someone re-ran the job by hand.

This guard reads the workflow and composite-action YAML directly rather than
trusting a checked-in list, so a newly added download step fails here instead of
silently reintroducing the flake.

What counts as a retry
----------------------
Structure, never the step's name. A retry is an `actions/download-artifact` step
whose `if:` is guarded on `steps.<id>.outcome == 'failure'` where `<id>` is
another download step **in the same job or composite**, and which fetches the
same artifact selector into the same path. A companion that guards on the right
outcome but fetches something else, or is not a download at all, does not
satisfy the pairing.

Where this differs from the upload guard
----------------------------------------
A retried *upload* must take a DISTINCT name: a failed finalize leaves an
invisible record holding the name for the rest of the run, so a same-named
retry 409s. Nothing equivalent happens on a download — a failed read burns
nothing — so the retry here reuses the first attempt's selector, and reusing it
is what this guard requires. The two rules look contradictory side by side and
are not; they are about opposite ends of the same call.

The backoff is shared, though: the 403 comes from an intermediary having a
moment, and a retry firing a second later lands inside the same window.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOWNLOAD_ACTION = "actions/download-artifact"

# `steps.<id>.outcome == 'failure'` — the only shape that makes a step a retry.
OUTCOME_GUARD_RE = re.compile(r"steps\.([A-Za-z0-9_-]+)\.outcome\s*==\s*'failure'")

# Workflow/action YAML this guard cannot parse. Must shrink, never grow.
UNPARSEABLE: set[str] = set()

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
        "workflows/conformance-upload-sarif.yaml",
        "conformance-${{ matrix.slug }}-sarif*",
    ): "workflow_run consumer; nothing gates on it and there is no queue entry to eject",
}


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


def _is_download(step: dict) -> bool:
    return DOWNLOAD_ACTION in str(step.get("uses", ""))


def _with(step: dict) -> dict:
    value = step.get("with")
    return value if isinstance(value, dict) else {}


def _selector(step: dict) -> str:
    """What the step asks the service for: a `pattern` glob or a bare `name`."""
    inputs = _with(step)
    return str(inputs.get("pattern") or inputs.get("name") or "")


def _target_path(step: dict) -> str:
    return str(_with(step).get("path", ""))


def _retry_target(step: dict) -> str | None:
    """The step id this download claims to retry, or None if it guards on nothing."""
    match = OUTCOME_GUARD_RE.search(str(step.get("if", "")))
    return match.group(1) if match else None


def _classify(steps: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Split a scope's downloads into (first_attempts, retries_by_target_id).

    A retry must guard on the outcome of another *download* step in this same
    scope. A download guarded on some non-download step's outcome is treated as
    a first attempt, so it still has to prove it has a retry of its own.
    """
    downloads = [s for s in steps if _is_download(s)]
    download_ids = {s.get("id") for s in downloads if s.get("id")}

    retries: dict[str, list[dict]] = {}
    first_attempts: list[dict] = []
    for step in downloads:
        target = _retry_target(step)
        if target and target in download_ids:
            retries.setdefault(target, []).append(step)
        else:
            first_attempts.append(step)
    return first_attempts, retries


def _live_first_attempts() -> list[tuple[str, str, dict, dict[str, list[dict]]]]:
    """Every non-exempt first-attempt download, with its scope's retry index."""
    live = []
    for rel, scope, steps in _scopes():
        first_attempts, retries = _classify(steps)
        for step in first_attempts:
            if (rel, _selector(step)) in EXEMPT:
                continue
            live.append((rel, scope, step, retries))
    return live


def _label(rel: str, scope: str, step: dict) -> str:
    return f"{rel} [{scope}] '{step.get('name') or step.get('uses')}'"


def _guards_on(step: dict, step_id: str) -> bool:
    return step_id in OUTCOME_GUARD_RE.findall(str(step.get("if", "")))


def test_no_new_unparseable_workflow_yaml():
    """Quarantine must shrink, never grow. An unparseable workflow is invisible
    to this guard — and GitHub cannot run it either."""
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


def test_the_guard_actually_finds_downloads_and_retries():
    """A scan that silently matched nothing would pass every assertion below.

    Asserts on both halves: if retry classification broke, `retries` would empty
    out while `first_attempts` grew, and the pairing test would then be
    asserting over the wrong set.
    """
    scopes = _scopes()
    assert len(scopes) > 50, "scope discovery collapsed"

    total_first, total_retries = 0, 0
    for _, _, steps in scopes:
        first_attempts, retries = _classify(steps)
        total_first += len(first_attempts)
        total_retries += sum(len(v) for v in retries.values())
    assert total_first >= 7, f"only {total_first} first-attempt downloads found"
    assert total_retries >= 3, f"only {total_retries} retry downloads found"


def test_every_gating_download_has_a_matching_retry_download():
    """The retry must be a real download of the same artifact, in the same scope."""
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
                f"{where}: no `{DOWNLOAD_ACTION}` step in this scope is guarded on "
                f"steps.{step_id}.outcome == 'failure'"
            )
            continue

        for companion in companions:
            if companion is step:
                problems.append(f"{where}: a step cannot be its own retry")
                continue
            if _selector(companion) != _selector(step):
                problems.append(
                    f"{where}: its retry fetches '{_selector(companion)}', not "
                    f"'{_selector(step)}' — so it is not a retry of this download"
                )
            if _target_path(companion) != _target_path(step):
                problems.append(
                    f"{where}: its retry unpacks to a different path "
                    f"({_target_path(companion)!r} != {_target_path(step)!r})"
                )

    assert not problems, (
        "Every artifact download on a gating path needs a companion "
        f"`{DOWNLOAD_ACTION}` step, in the same job/composite, guarded on the "
        "first attempt's outcome and fetching the same selector into the same "
        "path (see build-and-scan.yaml for the pattern) — or an EXEMPT entry "
        "with a reason:\n  " + "\n  ".join(problems)
    )


def test_first_attempt_never_fails_the_job_before_the_retry_runs():
    """Without continue-on-error the job dies and the retry never executes."""
    bad = [
        _label(rel, scope, step)
        for rel, scope, step, _ in _live_first_attempts()
        if step.get("continue-on-error") is not True
    ]
    assert not bad, (
        "The first download attempt must set `continue-on-error: true`, otherwise "
        "the job fails before its retry can run:\n  " + "\n  ".join(bad)
    )


def test_every_retry_waits_before_it_runs():
    """The 403 comes from an intermediary having a moment. A retry firing a
    second after the first attempt lands in the same window and both fail
    together — which is the one case that still reddens the job."""
    problems = []
    for rel, scope, steps in _scopes():
        _, retries = _classify(steps)
        for target_id, companions in retries.items():
            waits = [
                s
                for s in steps
                if "run" in s
                and "sleep" in str(s.get("run", ""))
                and _guards_on(s, target_id)
            ]
            if not waits:
                for companion in companions:
                    problems.append(
                        f"{_label(rel, scope, companion)}: no `run: sleep …` step "
                        f"guarded on steps.{target_id}.outcome == 'failure'"
                    )
    assert not problems, (
        "Each retry needs a backoff step ahead of it, guarded on the same "
        "outcome as the retry itself:\n  " + "\n  ".join(problems)
    )


def test_a_retry_is_not_identified_by_its_name():
    """Regression: naming a step '(retry)' must not exempt it from validation.

    Synthesised rather than read off disk, because the point is what the
    classifier does with a shape the repo should never contain.
    """
    steps = [
        {
            "name": "Download something (retry)",
            "uses": f"{DOWNLOAD_ACTION}@v8",
            "with": {"pattern": "thing*", "path": "/tmp"},
        }
    ]
    first_attempts, retries = _classify(steps)
    assert not retries, "a name substring must not make a step a retry"
    assert len(first_attempts) == 1, (
        "a step named '(retry)' with no outcome guard is an unretried first "
        "attempt and must still be validated"
    )


def test_a_companion_guarded_on_a_non_download_step_is_not_a_retry():
    """Regression: the guard must require the retry to be a download of its own."""
    steps = [
        {"id": "prep", "name": "Prep", "run": "make"},
        {
            "id": "download",
            "name": "Download",
            "continue-on-error": True,
            "uses": f"{DOWNLOAD_ACTION}@v8",
            "with": {"pattern": "thing*", "path": "/tmp"},
        },
        {
            # Guards on the right outcome but is not a download at all.
            "name": "Tell someone it failed",
            "if": "steps.download.outcome == 'failure'",
            "run": "echo oh no",
        },
    ]
    first_attempts, retries = _classify(steps)
    assert [s["id"] for s in first_attempts] == ["download"]
    assert not retries, "a non-download step must not count as the retry"


def test_a_companion_fetching_a_different_artifact_is_not_a_retry():
    """The pairing is on the artifact, not merely on the outcome guard.

    Two unrelated downloads in one job, the second guarded on the first's
    failure, would otherwise read as a retry and leave the first unhardened.
    """
    steps = [
        {
            "id": "download",
            "name": "Download the image",
            "continue-on-error": True,
            "uses": f"{DOWNLOAD_ACTION}@v8",
            "with": {"pattern": "docker-image*", "path": "/tmp"},
        },
        {
            "name": "Download the fallback bundle",
            "if": "steps.download.outcome == 'failure'",
            "uses": f"{DOWNLOAD_ACTION}@v8",
            "with": {"pattern": "something-else*", "path": "/tmp"},
        },
    ]
    first_attempts, retries = _classify(steps)
    companion = retries["download"][0]
    assert _selector(companion) != _selector(
        first_attempts[0]
    ), "the fixture must differ in selector, or it proves nothing"


def test_a_retry_in_another_job_does_not_satisfy_the_pairing():
    """`steps.<id>` only resolves within one job, so a retry in a sibling job is
    not a retry at all — it would reference an id that does not exist there."""
    doc = {
        "jobs": {
            "scanner": {
                "steps": [
                    {
                        "id": "download",
                        "name": "Download",
                        "continue-on-error": True,
                        "uses": f"{DOWNLOAD_ACTION}@v8",
                        "with": {"pattern": "thing*", "path": "/tmp"},
                    }
                ]
            },
            "elsewhere": {
                "steps": [
                    {
                        "name": "Download (retry)",
                        "if": "steps.download.outcome == 'failure'",
                        "uses": f"{DOWNLOAD_ACTION}@v8",
                        "with": {"pattern": "thing*", "path": "/tmp"},
                    }
                ]
            },
        }
    }
    scopes = _scopes_from_doc("synthetic.yaml", doc)
    assert len(scopes) == 2, "jobs must be separate scopes"

    by_scope = {scope: _classify(steps) for _, scope, steps in scopes}
    scanner_first, scanner_retries = by_scope["job scanner"]
    assert [s["id"] for s in scanner_first] == ["download"]
    assert not scanner_retries
    other_first, other_retries = by_scope["job elsewhere"]
    assert len(other_first) == 1
    assert not other_retries


def test_exemptions_still_point_at_real_downloads():
    """A stale exemption would silently excuse a file that no longer downloads."""
    live = set()
    for rel, _, steps in _scopes():
        first_attempts, _ = _classify(steps)
        live.update((rel, _selector(s)) for s in first_attempts)
    stale = sorted(set(EXEMPT) - live)
    assert not stale, f"EXEMPT entries no longer match any download step: {stale}"
