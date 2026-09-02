"""Did this PR's tests ever fail?

A test written alongside a fix proves nothing unless it would have failed
without the fix. That is the whole of red-green, and it is the one question
about test quality with a deterministic answer: take the tests this PR adds or
changes, run them against the *base* revision of the source, and see which ones
pass anyway. The ones that pass never captured the change. They are decoration,
and decoration is worse than an absent test because it buys confidence nobody
checked.

This matters more, not less, when the tests were written by a model. The
literature on LLM-authored tests is blunt: generated properties are frequently
trivial ("returns a value of the right type") or simply false. Meta drives its
generation loop with a mutation score for exactly this reason. Mutation testing
is the stronger instrument and it is also minutes per run; red-green answers the
sharper question — *did this test capture THIS change* — for one checkout and
one test run, which is what fits inside a review budget.

## The result classes, and why ERROR is a pass

Running new tests against old source has three interesting outcomes:

* **FAILED** — the test captured the change. This is the good case, and the
  point of the exercise.
* **ERROR / collection failure** — the test could not even import against the
  old source, because it references something this PR introduced. That is red
  in the strongest possible sense and counts as captured. Treating a collection
  error as "inconclusive" would mark every test of a new module as suspect,
  which is precisely backwards.
* **PASSED** — the test holds with or without the change. It is not a
  regression test for this PR. It may still be a perfectly good test of
  something else, which is why this is reported and never blocks.

## Why it never blocks

There are legitimate green-on-base tests: a test that documents existing
behaviour the PR must preserve, a refactor with no behavioural delta, a test
backfilling coverage of old code. A gate here would punish all three. The number
is reported, the reviewer reads it as a signal, and a human decides.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

#: Tests carried into one verification run. A PR that changes more test
#: functions than this is better served by the reviewer reading them than by a
#: long machine list, and the cap keeps the run inside the review budget.
MAX_TESTS = 60

CAPTURED = "captured"
NOT_CAPTURED = "not_captured"
INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class TestRef:
    """One test function this PR adds or changes."""

    path: str
    name: str

    @property
    def node_id(self) -> str:
        return f"{self.path}::{self.name}"


@dataclass(frozen=True)
class Outcome:
    test: TestRef
    status: str
    detail: str = ""


@dataclass
class Report:
    outcomes: list[Outcome] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def captured(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == CAPTURED]

    @property
    def not_captured(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == NOT_CAPTURED]

    @property
    def rate(self) -> float | None:
        """Share of examined tests that actually failed against base.

        None when nothing was examined — distinct from 0.0, which means every
        test examined passed on the old source. Collapsing the two would report
        a PR with no test changes as having the worst possible score.
        """
        graded = [o for o in self.outcomes if o.status in (CAPTURED, NOT_CAPTURED)]
        if not graded:
            return None
        return len([o for o in graded if o.status == CAPTURED]) / len(graded)


class CommandRunner(Protocol):
    def __call__(
        self, args: Sequence[str], cwd: Path
    ) -> subprocess.CompletedProcess: ...


def _run(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


# --------------------------------------------------------------------------
# Which tests to examine
# --------------------------------------------------------------------------


def changed_test_functions(repo: Path, files: Iterable) -> tuple[TestRef, ...]:
    """Test functions containing at least one line this PR added.

    Function-level rather than file-level on purpose. A PR that appends one test
    to a 900-line file has changed one test, and re-running the other forty
    against base would report a pile of green-on-base results that say nothing
    about this change — noise that would bury the one row that matters.
    """
    refs: list[TestRef] = []
    for changed in files:
        if changed.is_deleted or not changed.is_test or not changed.is_python:
            continue
        if not changed.added:
            continue
        try:
            tree = ast.parse((repo / changed.path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        added = set(changed.added)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            if any(node.lineno <= line <= end for line in added):
                refs.append(TestRef(path=changed.path, name=node.name))
    return tuple(refs[:MAX_TESTS])


# --------------------------------------------------------------------------
# Running them against the old source
# --------------------------------------------------------------------------

_RESULT = re.compile(
    r"^(?P<node>\S+::\S+)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED)", re.M
)


def parse_pytest_report(stdout: str) -> dict[str, str]:
    """`pytest -v` node id -> status.

    Parsed from the verbose line rather than from the exit code, because the run
    is expected to fail: a non-zero exit is the *success* condition here, and a
    caller keying on it would invert the entire result.
    """
    return {m.group("node"): m.group("status") for m in _RESULT.finditer(stdout)}


def classify(status: str | None) -> tuple[str, str]:
    """One pytest status -> what it says about the test.

    ERROR is `captured`, deliberately. A test that cannot import against the old
    source references something this PR introduced, which is red in the
    strongest sense available. Reading it as inconclusive would mark every test
    of a new module as suspect — exactly backwards.
    """
    if status == "FAILED":
        return CAPTURED, "failed against base — it captures this change"
    if status == "ERROR":
        return CAPTURED, "could not run against base — it references new code"
    if status == "PASSED":
        return NOT_CAPTURED, "passes against base — it does not capture this change"
    if status == "SKIPPED":
        return INCONCLUSIVE, "skipped against base"
    return INCONCLUSIVE, "no result reported against base"


def verify(
    *,
    repo: Path,
    base_ref: str,
    tests: Sequence[TestRef],
    workdir: Path,
    runner: CommandRunner = _run,
    copy_file: Callable[[Path, Path], None] | None = None,
) -> Report:
    """Run `tests` against `base_ref`'s source and report what each proves.

    The base tree is materialised in a throwaway location and the PR's *test*
    files are overlaid on it. Overlaying only the test files is the whole trick:
    keeping the old source and taking the new tests is exactly the state the
    author was in before they wrote the fix.

    Nothing is stashed and the working tree is never touched — a stash here
    would be shared with every other checkout on the machine.
    """
    report = Report()
    if not tests:
        report.skipped_reason = "no test functions added or changed in this PR"
        return report

    created = runner(
        ["git", "worktree", "add", "--detach", str(workdir), base_ref], repo
    )
    if created.returncode != 0:
        report.skipped_reason = (
            f"could not materialise base {base_ref}: {created.stderr.strip()[:200]}"
        )
        return report

    try:
        copier = copy_file or _copy
        for path in sorted({t.path for t in tests}):
            source = repo / path
            target = workdir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            copier(source, target)

        result = runner(
            [
                "uv",
                "run",
                "pytest",
                "-v",
                "--no-header",
                "-p",
                "no:randomly",
                *sorted({t.node_id for t in tests}),
            ],
            workdir,
        )
        statuses = parse_pytest_report(result.stdout)
        for test in tests:
            status, detail = classify(statuses.get(test.node_id))
            report.outcomes.append(Outcome(test=test, status=status, detail=detail))
    finally:
        runner(["git", "worktree", "remove", "--force", str(workdir)], repo)
    return report


def _copy(source: Path, target: Path) -> None:
    target.write_bytes(source.read_bytes())


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def render(report: Report) -> str:
    """The advisory section for the review summary.

    Phrased as a question to the author rather than a verdict, because there are
    legitimate reasons a test passes against base — documenting behaviour the PR
    must preserve, a refactor with no behavioural delta, backfilled coverage of
    old code. The number informs; a human decides.
    """
    if report.skipped_reason:
        return f"**Red-green:** not run — {report.skipped_reason}."

    rate = report.rate
    if rate is None:
        return "**Red-green:** no gradeable result."

    lines = [
        f"**Red-green:** {len(report.captured)}/{len(report.captured) + len(report.not_captured)}"
        f" of this PR's new tests fail against the base revision ({rate:.0%})."
    ]
    if report.not_captured:
        lines.append("")
        lines.append(
            "These pass with or without the change, so they are not regression "
            "tests for it. That can be deliberate — documenting behaviour the "
            "change must preserve, or backfilling old coverage — but if one was "
            "meant to capture this fix, it does not:"
        )
        lines.append("")
        lines += [f"- `{o.test.node_id}`" for o in report.not_captured]
    return "\n".join(lines)
