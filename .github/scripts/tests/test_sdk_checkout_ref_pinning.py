"""Every checkout of this repo from a workflow uses one of two refs.

Policy and rationale: "Reusing scripts from a reusable workflow" in
docs/standards/ci.md (FND-4905). A checkout of `atlanhq/application-sdk` uses
either the literal `ref: main` (the default) or `ref: ${{ job.workflow_sha }}`
(where this repo's own CI must exercise its own copy). Before this guard the
tree also had checkouts at a caller-supplied input and one with no ref at all.

What the guard rules out, and why:

- A caller-supplied ref. Every consumer calls these reusables `@main`; an input
  that picks another SDK ref is a pinning knob, and pinning is what the fleet
  is moving away from.
- An omitted ref. It reads as "main" but says nothing, and the next edit can
  make it something else.
- Any other expression, above all `github.job_workflow_sha`, which renders
  empty: checkout then reads `ref` as unsupplied and silently takes the default
  branch.

A deliberate exception carries an inline comment inside the step::

    # sdk-checkout-ref-exempt: <why>

and must also be on the reviewed list in this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTIONLINT = REPO_ROOT / ".github" / "actionlint.yaml"

SDK_REPO = "atlanhq/application-sdk"
ALLOWED_REFS = ("main", "${{ job.workflow_sha }}")
EXEMPT_MARKER = re.compile(r"#\s*sdk-checkout-ref-exempt:\s*(?P<reason>\S.*)$")
ACTIONLINT_IGNORE = 'property "workflow_sha" is not defined'

_STEP_START = re.compile(r"^(?P<indent>\s*)-\s")
_REPOSITORY_LINE = re.compile(
    r"^\s*repository:\s*['\"]?" + re.escape(SDK_REPO) + r"['\"]?\s*(#.*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Checkout:
    file: str
    job: str
    line: int
    with_: dict[str, Any]
    exempt_reason: str | None

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line} (job {self.job})"


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yaml"), *WORKFLOWS.glob("*.yml")])


def _sdk_checkouts_from_yaml(path: Path) -> list[tuple[str, dict[str, Any]]]:
    doc: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    jobs: dict[str, dict[str, Any] | None] = doc.get("jobs") or {}
    found: list[tuple[str, dict[str, Any]]] = []
    for job_id, job in jobs.items():
        steps: list[dict[str, Any]] = (job or {}).get("steps") or []
        for step in steps:
            with_: dict[str, Any] = step.get("with") or {}
            if str(step.get("uses", "")).startswith("actions/checkout@") and (
                str(with_.get("repository", "")).lower() == SDK_REPO
            ):
                found.append((job_id, with_))
    return found


def _step_blocks_from_text(path: Path) -> list[tuple[int, list[str]]]:
    """(1-based line of `repository:`, lines of the enclosing step) per SDK checkout.

    YAML comments do not survive parsing, so the exemption marker is read from
    the text. The step is the nearest `- ` above the `repository:` line, and runs
    until a non-blank line at or left of that dash's indent.
    """
    lines = path.read_text().splitlines()
    blocks: list[tuple[int, list[str]]] = []
    for idx, line in enumerate(lines):
        if not _REPOSITORY_LINE.match(line):
            continue
        start = next(i for i in range(idx, -1, -1) if _STEP_START.match(lines[i]))
        indent = len(_STEP_START.match(lines[start]).group("indent"))  # type: ignore[union-attr]
        end = idx + 1
        while end < len(lines):
            stripped = lines[end].strip()
            if stripped and len(lines[end]) - len(lines[end].lstrip()) <= indent:
                break
            end += 1
        blocks.append((idx + 1, lines[start:end]))
    return blocks


def _checkouts() -> list[Checkout]:
    out: list[Checkout] = []
    for path in _workflow_files():
        rel = str(path.relative_to(REPO_ROOT))
        parsed = _sdk_checkouts_from_yaml(path)
        blocks = _step_blocks_from_text(path)
        # The text scan and the parse must agree on how many checkouts exist,
        # or the marker lookup below could silently miss one (a quoted or
        # flow-style `repository:` the regex does not match).
        assert len(parsed) == len(blocks), (
            f"{rel}: YAML finds {len(parsed)} checkouts of {SDK_REPO}, the "
            f"text scan finds {len(blocks)}; teach _REPOSITORY_LINE the new shape"
        )
        for (job_id, with_), (line, block) in zip(parsed, blocks, strict=True):
            reasons = [
                m.group("reason") for b in block if (m := EXEMPT_MARKER.search(b))
            ]
            out.append(
                Checkout(rel, job_id, line, with_, reasons[0] if reasons else None)
            )
    return out


CHECKOUTS = _checkouts()


def test_the_scan_finds_the_checkouts() -> None:
    # A scan that finds nothing passes every assertion below vacuously.
    assert len(CHECKOUTS) > 40


def test_every_sdk_checkout_uses_main_or_job_workflow_sha() -> None:
    offenders = [
        f"{c.where}: ref={c.with_.get('ref')!r}"
        for c in CHECKOUTS
        if c.exempt_reason is None and c.with_.get("ref") not in ALLOWED_REFS
    ]
    assert not offenders, (
        f"checkouts of {SDK_REPO} must use `ref: main` or "
        "`ref: ${{ job.workflow_sha }}` (docs/standards/ci.md, 'Reusing scripts "
        "from a reusable workflow'); not a caller input, not omitted, not "
        "`github.job_workflow_sha`:\n  " + "\n  ".join(offenders)
    )


def test_only_the_reviewed_checkouts_are_exempt() -> None:
    # Adding an exemption should be a reviewed change to this list, not just a
    # comment in a workflow.
    exempt = sorted((c.file, c.job) for c in CHECKOUTS if c.exempt_reason is not None)
    assert exempt == [
        # The callback that completes the mirrored check on a dispatching SDK
        # PR: `application-sdk-ref` is set by this repo's own cross-repo
        # dispatch, not by a consumer, so the PR tests its own callback scripts.
        (".github/workflows/tests-reusable.yaml", "report-to-sdk"),
    ]


def test_every_workflow_using_job_workflow_sha_ignores_the_actionlint_false_positive() -> (
    None
):
    # actionlint <=1.7.12 models `job` without `workflow_sha` (see the FALSE
    # POSITIVE block in .github/actionlint.yaml). Without the per-file ignore the
    # pre-commit hook goes red on the first pinned checkout in a new file.
    config: dict[str, Any] = yaml.safe_load(ACTIONLINT.read_text())
    paths: dict[str, dict[str, list[str]] | None] = config.get("paths") or {}

    def ignores(rel: str) -> list[str]:
        return (paths.get(rel) or {}).get("ignore") or []

    missing: list[str] = []
    for p in _workflow_files():
        rel = str(p.relative_to(REPO_ROOT))
        if "job.workflow_sha" in p.read_text() and ACTIONLINT_IGNORE not in ignores(
            rel
        ):
            missing.append(rel)
    assert not missing, (
        f"add the workflow_sha ignore to .github/actionlint.yaml for: {missing}"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "      - name: x\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          repository: atlanhq/application-sdk\n"
            "          # sdk-checkout-ref-exempt: dispatched by our own CI\n"
            "          ref: ${{ inputs.x }}\n"
            "      # sdk-checkout-ref-exempt: belongs to the NEXT step\n"
            "      - name: y\n",
            "dispatched by our own CI",
        ),
        (
            "      - name: x\n"
            "        uses: actions/checkout@abc\n"
            "        with:\n"
            "          repository: atlanhq/application-sdk\n"
            "          ref: main\n"
            "      # sdk-checkout-ref-exempt: belongs to the NEXT step\n"
            "      - name: y\n",
            None,
        ),
    ],
    ids=["marker-inside-step", "marker-before-next-step-is-not-ours"],
)
def test_the_marker_is_scoped_to_its_own_step(
    tmp_path: Path, text: str, expected: str | None
) -> None:
    path = tmp_path / "w.yaml"
    path.write_text(text)
    [(line, block)] = _step_blocks_from_text(path)
    assert line == 4
    reasons = [m.group("reason") for b in block if (m := EXEMPT_MARKER.search(b))]
    assert (reasons[0] if reasons else None) == expected
