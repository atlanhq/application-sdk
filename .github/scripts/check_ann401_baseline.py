#!/usr/bin/env python3
"""Hold ruff's ANN401 (``typing.Any`` in a signature) to a baseline that only shrinks.

Why (FND-4301)
--------------
``Any`` in a parameter or return annotation switches type checking off at the
exact boundary a consumer types against. ``application_sdk`` had ~490 such
sites when ANN401 was enabled, so turning the rule on in ruff's ``select``
would have made every pre-commit run red. Instead this check runs ruff with
``--select ANN401`` over ``application_sdk/`` and compares the count per file
with ``.github/scripts/ann401_baseline.json``:

* a file's count ABOVE its baseline fails: a new ``Any`` in a signature.
  Annotate it with the real type, ``object`` (for a value the function only
  passes on or inspects), a ``TypeVar``, or a ``Protocol``.
* a file's count BELOW its baseline also fails, asking for ``--update``. Slack
  left in the baseline would let a later ``Any`` in the same file through.

The baseline is the only suppression: ruff runs with ``--ignore-noqa``, so a
``# noqa: ANN401`` hides nothing, and ``test_check_ann401_baseline.py`` fails
if ``pyproject.toml`` ignores ANN401 or sets a flake8-annotations option that
would (``allow-star-arg-any``, ``ignore-fully-untyped``, ...).

Comparing against the PR's own baseline is not enough: a PR could add an
``Any`` and raise the baseline in the same change. So, given ``--base-sha``,
the PR's baseline is also compared with the base revision's, and any file
whose count RISES fails. A file renamed in the PR carries its base count to
the new path. A base revision with no baseline (the PR that introduces it) is
measured instead: ruff runs on the base tree in a temporary ``git worktree``.

A run in which ruff reports no ANN401 at all fails rather than passes: a ruff
that analysed nothing would otherwise match an emptied baseline.

Usage:
    python .github/scripts/check_ann401_baseline.py                  # check (local)
    python .github/scripts/check_ann401_baseline.py --base-sha SHA   # check (CI)
    python .github/scripts/check_ann401_baseline.py --update         # rewrite baseline
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Callable, Mapping, Sequence, cast

# Run as a script, so its own directory is sys.path[0] and this resolves. The
# base-revision git handling is shared so a fix to it (FND-4987: never
# re-shallow a full clone) reaches both ratchets.
from check_pyright_baseline import (
    BaseRevisionError,
    _fetch_depth,
    _git,
    renamed_paths,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REL = ".github/scripts/ann401_baseline.json"
SOURCE_DIR = "application_sdk"
RULE = "ANN401"
#: ruff over the package takes a few seconds; a hang must not hold the runner.
RUFF_TIMEOUT_S = 300
UPDATE_COMMAND = (
    "uv run --frozen python .github/scripts/check_ann401_baseline.py --update"
)

#: Repo-relative file -> ANN401 count.
Counts = dict[str, int]
#: (file, line, column, message) for one finding.
Violation = tuple[str, int, int, str]
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class RuffRunError(RuntimeError):
    """ruff could not be run, or its output could not be read."""


def _ruff_cmd() -> list[str]:
    exe = shutil.which("ruff")
    return [exe] if exe else [sys.executable, "-m", "ruff"]


def measure(tree: Path = REPO_ROOT) -> tuple[Counts, list[Violation]]:
    """ANN401 findings under ``tree/application_sdk``, counted per file.

    Runs from ``tree`` so ruff reads that tree's own ``pyproject.toml``.
    """
    try:
        proc = subprocess.run(
            [
                *_ruff_cmd(),
                "check",
                "--no-cache",
                "--exit-zero",
                "--ignore-noqa",
                "--output-format=json",
                f"--select={RULE}",
                SOURCE_DIR,
            ],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=RUFF_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuffRunError(f"could not run ruff: {exc}") from exc
    if proc.returncode != 0:
        raise RuffRunError(f"ruff exited {proc.returncode}: {proc.stderr.strip()}")
    return parse_report(proc.stdout, tree)


def parse_report(stdout: str, tree: Path) -> tuple[Counts, list[Violation]]:
    """ruff's ``--output-format=json`` -> per-file counts and the findings."""
    try:
        items = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuffRunError(f"ruff did not print JSON: {exc}") from exc
    if not isinstance(items, list):
        raise RuffRunError("ruff's JSON report is not a list")
    root = tree.resolve()
    violations: list[Violation] = []
    for raw in cast("list[object]", items):
        if not isinstance(raw, dict):
            raise RuffRunError("ruff's JSON report has a non-object entry")
        item = cast("dict[str, object]", raw)
        if item.get("code") != RULE:
            continue
        location = cast("dict[str, int]", item["location"])
        rel = Path(str(item["filename"])).resolve().relative_to(root).as_posix()
        violations.append(
            (rel, location["row"], location["column"], str(item["message"]))
        )
    counts = Counter(file for file, _, _, _ in violations)
    return dict(sorted(counts.items())), sorted(violations)


def render_baseline(counts: Mapping[str, int]) -> str:
    return json.dumps(dict(sorted(counts.items())), indent=2) + "\n"


def parse_baseline(text: str) -> Counts:
    loaded: object = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError("baseline is not a JSON object")
    doc = cast("dict[object, object]", loaded)
    if not all(
        isinstance(k, str) and isinstance(n, int) and not isinstance(n, bool) and n > 0
        for k, n in doc.items()
    ):
        raise ValueError("baseline is not a file -> positive count map")
    return cast("Counts", doc)


def compare(
    baseline: Mapping[str, int], actual: Mapping[str, int]
) -> tuple[list[str], list[str]]:
    """(counts above the baseline, counts below it), one line per file."""
    above: list[str] = []
    below: list[str] = []
    for file in sorted(baseline.keys() | actual.keys()):
        was, now = baseline.get(file, 0), actual.get(file, 0)
        if now > was:
            above.append(f"{file}: {now} > baseline {was}")
        elif now < was:
            below.append(f"{file}: {now} < baseline {was}")
    return above, below


def raised_since_base(
    base: Mapping[str, int], head: Mapping[str, int], renames: Mapping[str, str]
) -> list[str]:
    """Files whose head-baseline count exceeds the base's (renames carried)."""
    carried: Counter[str] = Counter()
    for file, n in base.items():
        carried[renames.get(file, file)] += n
    return [
        f"{file}: baseline {n} > base revision's {carried.get(file, 0)}"
        for file, n in sorted(head.items())
        if n > carried.get(file, 0)
    ]


def read_base_baseline(base_sha: str, git: Runner = _git) -> Counts | None:
    """The base revision's baseline, or None when the base provably has none.

    Fails closed: a missing commit, a failed fetch or an unreadable tree raises.
    """
    if not base_sha:
        raise BaseRevisionError("--base-sha was given but is empty")
    if git(["cat-file", "-e", f"{base_sha}^{{commit}}"]).returncode != 0:
        fetch = git(["fetch", "--no-tags", *_fetch_depth(git), "origin", base_sha])
        if fetch.returncode != 0:
            raise BaseRevisionError(
                f"could not fetch base {base_sha}: {fetch.stderr.strip()}"
            )
    if git(["cat-file", "-e", f"{base_sha}^{{commit}}"]).returncode != 0:
        raise BaseRevisionError(f"base {base_sha} is not a commit in this checkout")
    listing = git(["ls-tree", "--full-tree", base_sha, "--", BASELINE_REL])
    if listing.returncode != 0:
        raise BaseRevisionError(
            f"could not list {BASELINE_REL} at {base_sha}: {listing.stderr.strip()}"
        )
    if not listing.stdout.strip():
        return None
    show = git(["show", f"{base_sha}:{BASELINE_REL}"])
    if show.returncode != 0:
        raise BaseRevisionError(f"could not read {BASELINE_REL} at {base_sha}")
    try:
        return parse_baseline(show.stdout)
    except ValueError as exc:
        raise BaseRevisionError(
            f"{BASELINE_REL} at {base_sha} is not a baseline: {exc}"
        ) from exc


def base_tree_counts(
    base_sha: str,
    git: Runner = _git,
    measure_tree: Callable[[Path], tuple[Counts, list[Violation]]] = measure,
) -> Counts:
    """ANN401 on the base revision's own tree, for a base with no baseline."""
    with tempfile.TemporaryDirectory(prefix="ann401-base-") as tmp:
        tree = Path(tmp) / "base"
        add = git(["worktree", "add", "--detach", str(tree), base_sha])
        if add.returncode != 0:
            raise BaseRevisionError(
                f"could not check out base {base_sha}: {add.stderr.strip()}"
            )
        try:
            return measure_tree(tree)[0]
        finally:
            git(["worktree", "remove", "--force", str(tree)])


def check_base(
    base_sha: str,
    head: Mapping[str, int],
    git: Runner = _git,
    measure_tree: Callable[[Path], tuple[Counts, list[Violation]]] = measure,
) -> bool:
    """Print and return whether the PR's baseline rises above the base's."""
    try:
        base = read_base_baseline(base_sha, git)
        source = f"the base revision's {BASELINE_REL}"
        if base is None:
            print(
                f"No {BASELINE_REL} at base {base_sha}: this PR introduces it. "
                "Comparing it with ruff's ANN401 findings on the base tree."
            )
            source = "ruff's ANN401 findings on the base tree"
            base = base_tree_counts(base_sha, git, measure_tree)
        renames = renamed_paths(base_sha, git, SOURCE_DIR)
    except (BaseRevisionError, RuffRunError) as exc:
        print(f"::error::{exc}")
        return True
    raised = raised_since_base(base, head, renames)
    if raised:
        print(
            f"::error::{len(raised)} ANN401 baseline count(s) rose above {source}. "
            "Type the new signature instead of raising the baseline."
        )
        for line in raised:
            print(f"  + {line}")
    return bool(raised)


def _offenders(lines: list[str], violations: list[Violation]) -> list[str]:
    files = {line.split(":", 1)[0] for line in lines}
    return [
        f"    {f}:{row}:{col} {msg}" for f, row, col, msg in violations if f in files
    ]


def main(
    argv: Sequence[str] | None = None,
    baseline_path: Path = REPO_ROOT / BASELINE_REL,
    measure_tree: Callable[[Path], tuple[Counts, list[Violation]]] = measure,
    git: Runner = _git,
) -> int:
    parser = argparse.ArgumentParser(
        description="Hold ruff ANN401 counts to a shrink-only per-file baseline."
    )
    parser.add_argument(
        "--update", action="store_true", help="rewrite the baseline from this run"
    )
    parser.add_argument(
        "--base-sha",
        help="base revision whose baseline this PR's baseline may not exceed",
    )
    args = parser.parse_args(argv)

    try:
        actual, violations = measure_tree(REPO_ROOT)
    except RuffRunError as exc:
        print(f"::error::{exc}")
        return 2
    if not actual:
        print(f"::error::ruff reported no {RULE} findings at all under {SOURCE_DIR}/")
        return 2

    if args.update:
        baseline_path.write_text(render_baseline(actual), encoding="utf-8")
        print(f"Wrote {baseline_path.name}: {sum(actual.values())} {RULE} sites")
        return 0

    baseline = parse_baseline(baseline_path.read_text(encoding="utf-8"))
    failed = False
    if args.base_sha is not None:
        failed = check_base(args.base_sha, baseline, git, measure_tree)
    above, below = compare(baseline, actual)
    print(
        f"{RULE} in {SOURCE_DIR}/: {sum(actual.values())} sites "
        f"(baseline {sum(baseline.values())})"
    )
    if above:
        print(
            f"::error::{len(above)} file(s) have more `Any` in a signature than "
            f"{BASELINE_REL} allows. Annotate the real type (or `object`, a "
            "TypeVar, a Protocol); do not raise the baseline."
        )
        for line in above:
            print(f"  + {line}")
        print(*_offenders(above, violations), sep="\n")
    if below:
        print(
            f"::error::{len(below)} file(s) have fewer ANN401 sites than the "
            f"baseline. Lock the improvement in: {UPDATE_COMMAND}"
        )
        for line in below:
            print(f"  - {line}")
    return 1 if above or below or failed else 0


if __name__ == "__main__":
    sys.exit(main())
