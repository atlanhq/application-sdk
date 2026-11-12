"""Every checkout of this repo from a workflow is pinned to `job.workflow_sha`.

Policy and rationale: "Reusing scripts from a reusable workflow" in
docs/standards/ci.md (FND-4905). A reusable's YAML is fixed at the commit the
caller resolved when the run started; `job.workflow_sha` is that commit. A
`ref: main` checkout resolves when the *step* runs, so a re-run days later, or
a run that straddles a merge, executes old YAML against new scripts. A
caller-supplied ref lets the caller choose the code that runs with the job's
secrets. Before this guard the tree had 61 such checkouts under four ref
policies (`job.workflow_sha`, `main`, a caller input, none).

The one exception is a *content* checkout: a file whose value must reflect
`main` whatever the caller resolved (the base vulnerability allowlist). It
opts out with an inline comment inside the step::

    # sdk-checkout-ref-exempt: <why this content must track main>

and an exempt checkout may not carry code: its ref must be a literal (an
expression can render empty, and checkout reads empty as "default branch"), and
its sparse set must not include `.github/scripts` or `.github/actions`. Code it
needs comes from a second, pinned checkout.
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
PINNED_REF = "${{ job.workflow_sha }}"
EXEMPT_MARKER = re.compile(r"#\s*sdk-checkout-ref-exempt:\s*(?P<reason>\S.*)$")
CODE_PATHS = (".github/scripts", ".github/actions")
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


def test_every_sdk_checkout_is_pinned_to_job_workflow_sha() -> None:
    offenders = [
        f"{c.where}: ref={c.with_.get('ref')!r}"
        for c in CHECKOUTS
        if c.exempt_reason is None and c.with_.get("ref") != PINNED_REF
    ]
    assert not offenders, (
        f"checkouts of {SDK_REPO} must use `ref: {PINNED_REF}` "
        "(docs/standards/ci.md, 'Reusing scripts from a reusable workflow'), or "
        "carry `# sdk-checkout-ref-exempt: <reason>` if they fetch content that "
        "must track main:\n  " + "\n  ".join(offenders)
    )


def _sparse_paths(with_: dict[str, Any]) -> list[str]:
    raw = with_.get("sparse-checkout")
    if raw is None:
        return []
    return [p.strip().lstrip("/") for p in str(raw).splitlines() if p.strip()]


@pytest.mark.parametrize(
    "checkout",
    [c for c in CHECKOUTS if c.exempt_reason is not None],
    ids=lambda c: c.where,
)
def test_an_exempt_checkout_fetches_content_not_code(checkout: Checkout) -> None:
    ref = checkout.with_.get("ref")
    assert isinstance(ref, str) and ref and "${{" not in ref, (
        f"{checkout.where}: an exempt checkout needs a literal ref; an expression "
        "can render empty and checkout then takes the default branch silently"
    )
    paths = _sparse_paths(checkout.with_)
    assert paths, f"{checkout.where}: an exempt checkout must be sparse (content only)"
    code = [
        p for p in paths if any(p.startswith(c) or c.startswith(p) for c in CODE_PATHS)
    ]
    assert not code, (
        f"{checkout.where}: exempt (unpinned) checkout carries code {code}; "
        f"fetch scripts from a second checkout at `{PINNED_REF}`"
    )


def test_only_the_content_checkouts_are_exempt() -> None:
    # Adding an exemption should be a reviewed change to this list, not just a
    # comment in a workflow.
    exempt = sorted((c.file, c.job) for c in CHECKOUTS if c.exempt_reason is not None)
    assert exempt == [
        (".github/workflows/build-and-scan.yaml", "security-gate"),
        (".github/workflows/trivy-container.yaml", "build"),
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
            "          # sdk-checkout-ref-exempt: allowlist tracks main\n"
            "          ref: main\n"
            "      # sdk-checkout-ref-exempt: belongs to the NEXT step\n"
            "      - name: y\n",
            "allowlist tracks main",
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
