"""Cross-file guard: artifact uploads go through `upload-artifact-retry`.

`actions/upload-artifact` treats the artifact service's finalize 403 as
non-retryable and fails the step on the first occurrence, even though the bytes
already landed. On a gating path that fails the job, fails the aggregate gate,
and ejects the merge-queue entry. The retry that absorbs it lives in one place,
`.github/actions/upload-artifact-retry`, and docs/standards/ci.md ("Artifacts
move through the retry composites") explains each of its rules.

This guard pins the composite's rules once, then checks that nothing in
.github/ calls `actions/upload-artifact` directly except:

* the composite itself;
* `EXEMPT` uploads, each with the reason it needs no retry;
* `AWAITING_COMPOSITE` files, whose hand-written first-attempt/retry pairs are
  checked against the same rules until they move to the composite.

It reads the workflow and composite-action YAML directly rather than trusting a
checked-in list, so a newly added direct upload fails here.

(Deliberately avoids naming sibling guards' scripts: this suite discovers call
sites by grepping .github/ for a script's name, and a prose mention here would
register this file as one of its callers.)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
UPLOAD_ACTION = "actions/upload-artifact@"
COMPOSITE = "actions/upload-artifact-retry/action.yaml"
COMPOSITE_REF = ".github/actions/upload-artifact-retry"
LOCAL_COMPOSITE_REF = f"./{COMPOSITE_REF}"

# What a retry appends to the first attempt's artifact name. Consumers resolve
# these artifacts by `<name>*` glob because of it.
RETRY_SUFFIX = "-retry"

# `steps.<id>.outcome == 'failure'`: the only shape that makes a step a retry.
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
        "workflows/actions-cost-report.yaml",
        "actions-cost-report",
    ): "weekly scheduled report; nothing gates on it, and the alert and "
    "Kryptonite publish do not read the artifact",
    (
        "actions/unit-tests/action.yaml",
        "unit-test-timings-${{ runner.os }}-${{ inputs.python-version }}"
        "${{ inputs.dependency-floors != '' && '-floors' || '' }}",
    ): "diagnostic per-test timings; continue-on-error, so a failed upload never "
    "fails the leg, and nothing reads the artifact to gate (FND-4304)",
    (
        "workflows/sdk-tests-reusable.yaml",
        "sdk-unit-test-coverage",
    ): "diagnostic coverage report; continue-on-error, so a failed upload never "
    "fails the leg, and the gate reads coverage.xml in the job, not the artifact "
    "(FND-4678)",
}

# Files that still write the pair out by hand. This repo's own PR CI runs
# conformance-reusable.yaml, and it reaches consumers too, so it must name the
# composite as `atlanhq/...@main`: until the composite is on main, that ref
# fails the job at setup. It moves once the composite merges.
AWAITING_COMPOSITE = {"workflows/conformance-reusable.yaml"}

# Longest retention any upload here may ask for. Codeql's 30 days is the current
# high-water mark; nothing in this repo reads an artifact a month later.
MAX_RETENTION_DAYS = 30

Step = dict
Scope = tuple[str, str, list[Step]]


def _yaml_files() -> list[Path]:
    files = sorted((ROOT / "workflows").glob("*.y*ml"))
    files += sorted((ROOT / "actions").glob("*/action.y*ml"))
    return files


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _scopes_from_doc(rel: str, doc: dict) -> list[Scope]:
    """(file, scope, steps) per job and per composite `runs` block.

    `steps.<id>` only resolves within one job, so a retry in a different job of
    the same file must not satisfy another job's pairing.
    """
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


def _docs() -> dict[str, dict]:
    docs = {}
    for path in _yaml_files():
        doc = yaml.safe_load(path.read_text())
        if isinstance(doc, dict):
            docs[_rel(path)] = doc
    return docs


def _scopes() -> list[Scope]:
    return [s for rel, doc in _docs().items() for s in _scopes_from_doc(rel, doc)]


def _is_direct_upload(step: Step) -> bool:
    return UPLOAD_ACTION in str(step.get("uses", ""))


def _is_composite_call(step: Step) -> bool:
    return str(step.get("uses", "")).split("@")[0].endswith(COMPOSITE_REF)


def _with(step: Step) -> dict:
    value = step.get("with")
    return value if isinstance(value, dict) else {}


def _artifact_name(step: Step) -> str:
    return str(_with(step).get("name", ""))


def _artifact_path(step: Step) -> str:
    """Normalised so a multi-line `path:` block compares equal across steps."""
    raw = str(_with(step).get("path", ""))
    return "\n".join(line.strip() for line in raw.splitlines() if line.strip())


def _label(rel: str, scope: str, step: Step) -> str:
    return f"{rel} [{scope}] '{step.get('name') or step.get('uses')}'"


def _guards_on(step: Step, step_id: str) -> bool:
    return step_id in OUTCOME_GUARD_RE.findall(str(step.get("if", "")))


def _composite_steps() -> list[Step]:
    doc = yaml.safe_load((ROOT / COMPOSITE).read_text())
    return doc["runs"]["steps"]


def _composite_calls() -> list[tuple[str, str, Step]]:
    return [
        (rel, scope, step)
        for rel, scope, steps in _scopes()
        for step in steps
        if _is_composite_call(step)
    ]


# ── The composite ───────────────────────────────────────────────────────────


def test_the_composite_uploads_retries_under_a_distinct_name_after_a_wait():
    """The four rules, pinned on the one implementation."""
    first, wait, retry = _composite_steps()

    assert _is_direct_upload(first) and first.get("id"), "first step is the upload"
    assert first.get("continue-on-error") is True, (
        "the first attempt must not fail the step before the retry runs"
    )
    assert str(first.get("if")) == "always()", (
        "the caller's `if:` is the only gate: an `if:` here would skip the upload "
        "whenever an earlier step of the caller's job failed"
    )

    assert "sleep 20" in str(wait.get("run")), "the retry needs a backoff"
    assert _guards_on(wait, first["id"]) and "always()" in str(wait.get("if"))

    assert _is_direct_upload(retry)
    assert _guards_on(retry, first["id"]) and "always()" in str(retry.get("if"))
    assert retry.get("continue-on-error") is not True, (
        "losing both attempts must fail the step; a caller that tolerates it "
        "sets continue-on-error on its own step"
    )

    assert _artifact_name(first) == "${{ inputs.name }}"
    assert _artifact_name(retry) == "${{ inputs.name }}" + RETRY_SUFFIX, (
        "a failed finalize burns the first attempt's name for the run, so a "
        "same-named retry 409s"
    )
    for step in (first, retry):
        inputs = _with(step)
        assert inputs.get("overwrite") is True, (
            "both attempts overwrite, so a re-run leaves one live artifact per name"
        )
        assert inputs.get("path") == "${{ inputs.path }}"
        assert inputs.get("retention-days") == "${{ inputs.retention-days }}"
        assert inputs.get("if-no-files-found") == "${{ inputs.if-no-files-found }}"


def test_the_composite_requires_a_retention():
    """No default: an upload without one lives for the 90-day repo default."""
    doc = yaml.safe_load((ROOT / COMPOSITE).read_text())
    retention = doc["inputs"]["retention-days"]
    assert retention.get("required") is True and "default" not in retention


# ── Who uploads, and how ────────────────────────────────────────────────────


def test_the_scan_finds_composite_calls_and_exempt_uploads():
    """A scan that silently matched nothing would pass every assertion below."""
    assert len(_scopes()) > 50, "scope discovery collapsed"
    assert len(_composite_calls()) >= 15, "composite call discovery collapsed"


def test_no_direct_upload_outside_the_composite():
    """New uploads use the composite; the retry rules live there, not here."""
    problems = []
    for rel, scope, steps in _scopes():
        if rel == COMPOSITE or rel in AWAITING_COMPOSITE:
            continue
        for step in steps:
            if _is_direct_upload(step) and (rel, _artifact_name(step)) not in EXEMPT:
                problems.append(_label(rel, scope, step))
    assert not problems, (
        f"Use `atlanhq/application-sdk/{COMPOSITE_REF}@main` instead of "
        f"`actions/upload-artifact`, or add an EXEMPT entry with the reason the "
        "upload needs no retry:\n  " + "\n  ".join(problems)
    )


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


def test_every_upload_sets_an_explicit_bounded_retention():
    """No upload may fall back to the repo default (FND-3313).

    Without `retention-days` an artifact lives for the repo/org default, which
    is 90 days. The conformance SARIF uploads shipped that way: 12 per run, every
    one of them held for three months in each connector repo.
    """
    problems = []
    for rel, scope, steps in _scopes():
        if rel == COMPOSITE:
            continue
        for step in steps:
            if not (_is_direct_upload(step) or _is_composite_call(step)):
                continue
            days = _with(step).get("retention-days")
            # bool is an int subclass, so `retention-days: true` would pass as 1.
            if (
                isinstance(days, bool)
                or not isinstance(days, int)
                or not 1 <= days <= MAX_RETENTION_DAYS
            ):
                problems.append(f"{_label(rel, scope, step)}: retention-days={days!r}")
    assert not problems, (
        "Every upload must set an integer `retention-days` between 1 and "
        f"{MAX_RETENTION_DAYS}:\n  " + "\n  ".join(problems)
    )


def test_no_workflow_uploads_an_image_tarball():
    """`docker-image` was ~97% of each connector repo's live artifact bytes.

    Measured over three connector repos (FND-3313): 50-76 tarballs of
    ~0.55-0.65 GB each live under the old 7-day retention, 27-49 GB per repo.
    Its only readers were the Trivy and Endor jobs of the same run, so
    build-and-scan.yaml now builds and scans in one job and the tarball never
    leaves the runner (FND-3319). Any image tarball upload is a regression.
    """
    found = [
        _label(rel, scope, step)
        for rel, scope, steps in _scopes()
        for step in steps
        if (_is_direct_upload(step) or _is_composite_call(step))
        and (
            _artifact_path(step).endswith(".tar")
            or _artifact_name(step).startswith("docker-image")
        )
    ]
    assert not found, "image tarball uploads:\n  " + "\n  ".join(found)


def test_exemptions_still_point_at_real_uploads():
    """A stale exemption would silently excuse a file that no longer uploads."""
    live = {
        (rel, _artifact_name(step))
        for rel, _, steps in _scopes()
        for step in steps
        if _is_direct_upload(step)
    }
    stale = sorted(set(EXEMPT) - live)
    assert not stale, f"EXEMPT entries no longer match any upload step: {stale}"


# ── Hand-written pairs awaiting the composite ───────────────────────────────


def _awaiting_pairs() -> list[tuple[str, str, Step, list[Step], list[Step]]]:
    """(file, scope, first attempt, its retries, the scope's steps)."""
    pairs = []
    for rel, scope, steps in _scopes():
        if rel not in AWAITING_COMPOSITE:
            continue
        uploads = [s for s in steps if _is_direct_upload(s)]
        ids = {s.get("id") for s in uploads if s.get("id")}
        firsts = [
            s
            for s in uploads
            if not set(OUTCOME_GUARD_RE.findall(str(s.get("if", "")))) & ids
        ]
        for first in firsts:
            retries = [
                s for s in uploads if first.get("id") and _guards_on(s, first["id"])
            ]
            pairs.append((rel, scope, first, retries, steps))
    return pairs


def test_awaiting_files_still_have_pairs():
    """Once a file has moved to the composite, take it off AWAITING_COMPOSITE."""
    found = {rel for rel, *_ in _awaiting_pairs()}
    assert found == AWAITING_COMPOSITE, (
        f"no hand-written uploads left in {sorted(AWAITING_COMPOSITE - found)}"
    )


def test_awaiting_pairs_follow_the_composites_rules():
    """The same four rules the composite pins, on each hand-written pair."""
    problems = []
    for rel, scope, first, retries, steps in _awaiting_pairs():
        where = _label(rel, scope, first)
        if first.get("continue-on-error") is not True:
            problems.append(f"{where}: first attempt is not continue-on-error")
        if len(retries) != 1:
            problems.append(f"{where}: {len(retries)} retries guarded on its outcome")
            continue
        (retry,) = retries
        if _artifact_name(retry) != _artifact_name(first) + RETRY_SUFFIX:
            problems.append(f"{where}: retry uploads '{_artifact_name(retry)}'")
        if _artifact_path(retry) != _artifact_path(first):
            problems.append(f"{where}: retry uploads a different path")
        for step in (first, retry):
            if _with(step).get("overwrite") is not True:
                problems.append(f"{_label(rel, scope, step)}: no `overwrite: true`")
        if _with(retry).get("retention-days") != _with(first).get("retention-days"):
            problems.append(f"{where}: retry keeps the artifact for a different time")
        if not any(
            "sleep" in str(s.get("run", "")) and _guards_on(s, first["id"])
            for s in steps
        ):
            problems.append(f"{where}: no `sleep` step guarded on its outcome")
    assert not problems, "\n  ".join(problems)


# ── Readers of retried artifacts ────────────────────────────────────────────


def _retried_literal_names() -> set[str]:
    """Artifact names that have a retry, expression-free so they can be grepped."""
    names = {_artifact_name(step) for _, _, step in _composite_calls()}
    names |= {
        _artifact_name(first)
        for _, _, first, retries, _ in _awaiting_pairs()
        if retries
    }
    return {n for n in names if n and "${{" not in n}


def _bare_name_addresses(text: str, name: str) -> int:
    """How many times `text` uses `name` as a whole token, i.e. as an address.

    Artifact names collide with filenames and flags built from the same words
    (`trivy-results.json`, `--trivy-results`, `docker-image://…`), none of
    which read an artifact. So the name must stand alone: no word character,
    dot or dash on the left, and no word character, dot, colon, slash or dash
    on the right. Excluding a trailing dash is also what stops the retry name
    from registering as a mention of its own base.

    The one dash allowed on the left is a shell default (`${NAME:-<base>}`),
    which is an artifact address like any other.
    """
    pattern = rf"(?:(?<![\w.-])|(?<=:-)){re.escape(name)}(?![\w.:/-])"
    return len(re.findall(pattern, text))


def _addresses_by_bare_name(text: str, name: str) -> bool:
    return _bare_name_addresses(text, name) > 0


def _consumer_files() -> list[Path]:
    """Everything in .github/ that could address an artifact by name.

    Workflows and composites cover `download-artifact` steps and `run:` blocks;
    the scripts cover the `gh run download` / artifact-listing callers. This
    suite's own files are excluded: they name artifacts as fixtures.
    """
    return _yaml_files() + sorted((ROOT / "scripts").glob("*.py"))


def test_no_consumer_addresses_a_retried_artifact_by_base_name_alone():
    """A reader of a retried artifact has to accept the retry name too.

    A `download-artifact` glob (`<name>*`), a `--name` resolved from the run's
    artifact listing, or a jq/regex that accepts either. A file that mentions
    the base name and nowhere mentions the retry name or a glob of it is reading
    only the first attempt's artifact, which is exactly the artifact that is
    missing whenever the retry was needed. Both cross-run
    `gh run download --name trivy-results` callers shipped in that state.
    """
    names = _retried_literal_names()
    assert len(names) >= 5, f"retried-name discovery collapsed: {sorted(names)}"

    # The composite call that uploads a name mentions it in `with.name`, and
    # maybe in its step name; that is the writer, not a reader.
    writers: dict[tuple[str, str], int] = {}
    for rel, _, step in _composite_calls():
        name = _artifact_name(step)
        own = 1 + _bare_name_addresses(str(step.get("name", "")), name)
        writers[(rel, name)] = writers.get((rel, name), 0) + own

    problems = []
    for path in _consumer_files():
        text = path.read_text()
        for name in sorted(names):
            own = writers.get((_rel(path), name), 0)
            if _bare_name_addresses(text, name) <= own:
                continue
            if f"{name}{RETRY_SUFFIX}" in text or f"{name}*" in text:
                continue
            problems.append(f"{_rel(path)}: names '{name}' with no retry-aware read")
    assert not problems, (
        "A consumer of a retried artifact must accept the retry name too: glob "
        f"`<name>*`, resolve `--name` from the run's artifact listing, or match "
        f"`<name>{RETRY_SUFFIX}` explicitly:\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize(
    "text",
    [
        'gh run download "$RUN_ID" --name trivy-results --dir /tmp',
        "          name: trivy-results\n",
        "--jq '.artifacts[] | select(.name == \"trivy-results\")'",
        "${ARTIFACT_NAME:-trivy-results}",
    ],
)
def test_the_bare_name_matcher_catches_an_artifact_address(text: str):
    assert _addresses_by_bare_name(text, "trivy-results")


@pytest.mark.parametrize(
    "text",
    [
        "--output trivy-results.json",  # a file it writes, not an artifact
        "python3 check_allowlist.py --trivy-results results.json",  # a flag
        "gh run download --name trivy-results-retry",  # the retry's own name
        "trivy-container-results.json",  # a different artifact entirely
    ],
)
def test_the_bare_name_matcher_ignores_lookalikes(text: str):
    assert not _addresses_by_bare_name(text, "trivy-results")
