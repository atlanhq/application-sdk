#!/usr/bin/env python3
"""Hold ruff's C901 and PLR0915 over ``application_sdk/`` to a baseline that only shrinks.

Why (FND-4266)
--------------
``docs/standards/coding.md`` asks for small functions, but nothing measured
them: ``application_sdk`` had 126 functions over ruff's C901 (McCabe complexity
above 10) or PLR0915 (more than 50 statements) when this check was added.
Putting both rules in ruff's ``select`` would make every pre-commit run red, and
a ``per-file-ignores`` entry would let a file that has one long function grow
any number of new ones. Instead this check runs ruff with
``--select C901,PLR0915`` over ``application_sdk/`` and compares the over-limit
functions, by file, rule and qualified name, with
``.github/scripts/complexity_baseline.json``:

* a function NOT in the baseline fails: a new over-limit function, or one that
  grew past a threshold. Split it.
* a baseline function that is no longer over the limit also fails, asking for
  ``--update``. A stale entry would let a later function of that name through.

The baseline is the only suppression. ruff runs with ``--isolated``, so no
configuration file is read: no ``ignore`` / ``per-file-ignores``, no
``exclude``, no nested ``ruff.toml`` and no raised ``max-complexity`` /
``max-statements`` can hide a function. ``--ignore-noqa`` does the same for
``# noqa: C901``. The thresholds are passed on the command line (ruff's
defaults, 10 and 50).

Comparing against the PR's own baseline is not enough: a PR could add a long
function and add it to the baseline in the same change. So, given
``--base-sha``, every entry in the PR's baseline must also be in the base
revision's, in the same file. A module split that moves an over-limit function
to a new file unchanged passes: a base entry carries to another file only when
its own file no longer defines a function of that name AND the function's AST
(formatting and comments aside) is identical in the base and the new file. A
same-named replacement, or a function edited while it moves, counts as new;
move it unchanged and change it in a later PR. Renaming a function counts as a
new one. A base revision with no baseline (the PR that introduces it) is
measured instead: ruff runs on the base tree in a temporary ``git worktree``.

A run in which ruff reports nothing is accepted only if ruff still flags a
canary function that breaks both limits, so a ruff that stopped reporting the
rules cannot match an emptied baseline, while a fully paid-down tree can.

Usage:
    python .github/scripts/check_complexity_baseline.py                  # check (local)
    python .github/scripts/check_complexity_baseline.py --base-sha SHA   # check (CI)
    python .github/scripts/check_complexity_baseline.py --update         # lock in a reduction
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence, cast

# Run as a script, so its own directory is sys.path[0] and this resolves. The
# base-revision git handling is shared with the other ratchets (FND-4987:
# never re-shallow a full clone).
from check_pyright_baseline import BaseRevisionError, _fetch_depth, _git

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REL = ".github/scripts/complexity_baseline.json"
SOURCE_DIR = "application_sdk"
RULES = ("C901", "PLR0915")
#: ruff's defaults, passed explicitly: ``--isolated`` reads no config, and a
#: raised threshold would empty the baseline without splitting anything.
MAX_COMPLEXITY = 10
MAX_STATEMENTS = 50
#: ``--isolated`` reads no config, so the target version is passed explicitly.
#: Held equal to ``requires-python`` by the test suite.
TARGET_VERSION = "py311"
#: A function over both limits: 11 branches, 62 statements.
CANARY = (
    "def f(x):\n"
    + "".join(f"    if x == {i}:\n        x += 1\n" for i in range(11))
    + "".join(f"    y{i} = x\n" for i in range(40))
    + "    return x\n"
)
#: ruff over the package takes a few seconds; a hang must not hold the runner.
RUFF_TIMEOUT_S = 300
UPDATE_COMMAND = (
    "uv run --frozen python .github/scripts/check_complexity_baseline.py --update"
)

#: Repo-relative file -> rule -> sorted qualified names of over-limit functions.
Baseline = dict[str, dict[str, list[str]]]
#: (file, line, rule, qualified name, ruff message) for one finding.
Violation = tuple[str, int, str, str, str]
Measure = Callable[[Path], tuple[Baseline, list[Violation]]]
#: Repo-relative file -> its source in one revision, or None when it is absent
#: or unreadable there.
SourceOf = Callable[[str], "str | None"]
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class RuffRunError(RuntimeError):
    """ruff could not be run, or its output could not be read."""


def _ruff_cmd() -> list[str]:
    exe = shutil.which("ruff")
    return [exe] if exe else [sys.executable, "-m", "ruff"]


def measure(tree: Path = REPO_ROOT) -> tuple[Baseline, list[Violation]]:
    """C901/PLR0915 findings under ``tree/application_sdk``, by file and rule.

    ``--isolated``: no config file can suppress, exclude or re-scope a finding.
    """
    try:
        proc = subprocess.run(
            [
                *_ruff_cmd(),
                "check",
                "--no-cache",
                "--exit-zero",
                "--isolated",
                "--ignore-noqa",
                f"--target-version={TARGET_VERSION}",
                f"--config=lint.mccabe.max-complexity={MAX_COMPLEXITY}",
                f"--config=lint.pylint.max-statements={MAX_STATEMENTS}",
                "--output-format=json",
                f"--select={','.join(RULES)}",
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


def _functions(source: str) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """(dotted qualified name, node) for every function in ``source``."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((prefix + child.name, child))
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(ast.parse(source), "")
    return found


def qualnames(source: str) -> dict[int, str]:
    """``{def line: dotted qualified name}`` for every function in ``source``."""
    return {node.lineno: name for name, node in _functions(source)}


def fingerprints(source: str | None) -> dict[str, set[str]] | None:
    """``{qualified name: AST dumps}`` for ``source``; None if it is absent or broken.

    The dump leaves out positions, so formatting and comments do not count:
    a function moved unchanged has the same fingerprint in its new file.
    """
    if source is None:
        return None
    try:
        functions = _functions(source)
    except (SyntaxError, ValueError):
        return None
    out: dict[str, set[str]] = {}
    for name, node in functions:
        out.setdefault(name, set()).add(ast.dump(node, include_attributes=False))
    return out


def parse_report(stdout: str, tree: Path) -> tuple[Baseline, list[Violation]]:
    """ruff's ``--output-format=json`` -> the baseline shape and the findings.

    Each finding is named by the qualified name of the function whose ``def``
    is on the reported line, read from the source ruff analysed.
    """
    try:
        items = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuffRunError(f"ruff did not print JSON: {exc}") from exc
    if not isinstance(items, list):
        raise RuffRunError("ruff's JSON report is not a list")
    root = tree.resolve()
    names_by_file: dict[str, dict[int, str]] = {}
    violations: list[Violation] = []
    for raw in cast("list[object]", items):
        if not isinstance(raw, dict):
            raise RuffRunError("ruff's JSON report has a non-object entry")
        item = cast("dict[str, object]", raw)
        rule = item.get("code")
        if rule not in RULES:
            continue
        path = Path(str(item["filename"])).resolve()
        rel = path.relative_to(root).as_posix()
        if rel not in names_by_file:
            names_by_file[rel] = qualnames(path.read_text(encoding="utf-8"))
        row = cast("dict[str, int]", item["location"])["row"]
        name = names_by_file[rel].get(row)
        if name is None:
            raise RuffRunError(f"{rel}:{row}: {rule} is not on a function definition")
        violations.append((rel, row, str(rule), name, str(item["message"])))
    violations.sort()
    found: Baseline = {}
    for file, _, rule, name, _ in violations:
        found.setdefault(file, {}).setdefault(rule, []).append(name)
    return canonical(found), violations


def canonical(baseline: Mapping[str, Mapping[str, Sequence[str]]]) -> Baseline:
    """Files, rules and names sorted; empty entries dropped."""
    out: Baseline = {}
    for file in sorted(baseline):
        rules = {r: sorted(n) for r, n in sorted(baseline[file].items()) if n}
        if rules:
            out[file] = rules
    return out


def ruff_reports_both_rules(measure_tree: Measure = measure) -> bool:
    """Whether ruff, as this check runs it, flags a function over both limits."""
    with tempfile.TemporaryDirectory(prefix="complexity-canary-") as tmp:
        source = Path(tmp) / SOURCE_DIR
        source.mkdir()
        (source / "canary.py").write_text(CANARY, encoding="utf-8")
        expected = {f"{SOURCE_DIR}/canary.py": {rule: ["f"] for rule in RULES}}
        return measure_tree(Path(tmp))[0] == expected


def render_baseline(baseline: Mapping[str, Mapping[str, Sequence[str]]]) -> str:
    return json.dumps(canonical(baseline), indent=2) + "\n"


def is_source_key(file: str) -> bool:
    """Whether ``file`` is a normalised ``application_sdk/**.py`` repo path.

    Baseline keys come from the PR, and the base check reads the files they
    name: no absolute path, no ``..``, nothing outside the source directory.
    """
    path = PurePosixPath(file)
    return (
        path.as_posix() == file
        and not path.is_absolute()
        and path.parts[:1] == (SOURCE_DIR,)
        and len(path.parts) > 1
        and ".." not in path.parts
        and path.suffix == ".py"
    )


def parse_baseline(text: str) -> Baseline:
    loaded: object = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError("baseline is not a JSON object")
    for file, rules in cast("dict[object, object]", loaded).items():
        if not isinstance(file, str) or not is_source_key(file):
            raise ValueError(f"{file!r} is not an {SOURCE_DIR}/**.py path")
        if not isinstance(rules, dict) or not rules:
            raise ValueError(f"{file!r} does not map to a non-empty rule table")
        for rule, names in cast("dict[object, object]", rules).items():
            if rule not in RULES:
                raise ValueError(f"{file}: {rule!r} is not one of {RULES}")
            if (
                not isinstance(names, list)
                or not names
                or not all(isinstance(n, str) and n for n in names)
            ):
                raise ValueError(f"{file} {rule}: not a non-empty list of names")
    return cast("Baseline", loaded)


def _entries(baseline: Mapping[str, Mapping[str, Sequence[str]]]) -> Counter[str]:
    """Every (file, rule, name) as a printable line, with its multiplicity."""
    return Counter(
        f"{file} {rule} {name}"
        for file, rules in baseline.items()
        for rule, names in rules.items()
        for name in names
    )


def compare(baseline: Baseline, actual: Baseline) -> tuple[list[str], list[str]]:
    """(over-limit functions not in the baseline, baseline entries no longer over)."""
    want, got = _entries(baseline), _entries(actual)
    return sorted((got - want).elements()), sorted((want - got).elements())


def tree_source(tree: Path = REPO_ROOT) -> SourceOf:
    """Read a source file from a working tree.

    Only a regular file that resolves inside ``tree`` is read: a key that
    escapes it (by path or symlink), or names a device or FIFO, reads as absent.
    """
    root = tree.resolve()

    def source(file: str) -> str | None:
        if not is_source_key(file):
            return None
        try:
            path = (root / file).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                return None
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    return source


def git_source(base_sha: str, git: Runner = _git) -> SourceOf:
    """Read a file at ``base_sha``."""

    def source(file: str) -> str | None:
        show = git(["show", f"{base_sha}:{file}"])
        return show.stdout if show.returncode == 0 else None

    return source


def _triples(baseline: Baseline) -> list[tuple[str, str, str]]:
    return [
        (file, rule, name)
        for file, rules in canonical(baseline).items()
        for rule, names in rules.items()
        for name in names
    ]


def raised_since_base(
    base: Baseline, head: Baseline, base_source: SourceOf, head_source: SourceOf
) -> list[str]:
    """Entries of ``head`` that no entry of ``base`` accounts for.

    An entry is accounted for by the same (file, rule, name) in ``base``. A
    leftover base entry accounts for one in another file only when the function
    provably moved there unchanged: its old file no longer defines that name
    (``head_source``), and its AST at base (``base_source``) is identical to the
    one in the new file. Anything else, a same-named replacement or a function
    edited on the way, is a new function. Unreadable sources never carry.
    """
    left = Counter(_triples(base))
    unmatched: list[tuple[str, str, str]] = []
    for entry in _triples(head):
        if left[entry] > 0:
            left[entry] -= 1
        else:
            unmatched.append(entry)

    cache: dict[tuple[str, str], dict[str, set[str]] | None] = {}

    def prints(side: str, file: str) -> dict[str, set[str]] | None:
        if (side, file) not in cache:
            read = base_source if side == "base" else head_source
            cache[(side, file)] = fingerprints(read(file))
        return cache[(side, file)]

    def moved_here(old: str, new: str, name: str) -> bool:
        old_head = prints("head", old)
        if old_head is None and head_source(old) is not None:
            return False  # the old file is there but unreadable: fail closed
        if old_head is not None and name in old_head:
            return False  # still defined where it was: paid down, not moved
        old_base = prints("base", old)
        if not old_base or name not in old_base:
            return False
        new_head = prints("head", new)
        return new_head is not None and bool(old_base[name] & new_head.get(name, set()))

    raised: list[str] = []
    for file, rule, name in unmatched:
        source = next(
            (
                key
                for key, n in left.items()
                if n > 0 and key[1:] == (rule, name) and moved_here(key[0], file, name)
            ),
            None,
        )
        if source is None:
            raised.append(f"{file} {rule} {name}")
        else:
            left[source] -= 1
    return raised


def read_base_baseline(base_sha: str, git: Runner = _git) -> Baseline | None:
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


def base_tree_findings(
    base_sha: str, git: Runner = _git, measure_tree: Measure = measure
) -> Baseline:
    """C901/PLR0915 on the base revision's own tree, for a base with no baseline."""
    with tempfile.TemporaryDirectory(prefix="complexity-base-") as tmp:
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
    head: Baseline,
    git: Runner = _git,
    measure_tree: Measure = measure,
    head_source: SourceOf | None = None,
) -> bool:
    """Print and return whether the PR's baseline adds a function the base lacks."""
    try:
        base = read_base_baseline(base_sha, git)
        source = f"the base revision's {BASELINE_REL}"
        if base is None:
            print(
                f"No {BASELINE_REL} at base {base_sha}: this PR introduces it. "
                "Comparing it with ruff's findings on the base tree."
            )
            source = "ruff's C901/PLR0915 findings on the base tree"
            base = base_tree_findings(base_sha, git, measure_tree)
    except (BaseRevisionError, RuffRunError) as exc:
        print(f"::error::{exc}")
        return True
    raised = raised_since_base(
        base, head, git_source(base_sha, git), head_source or tree_source()
    )
    if raised:
        print(
            f"::error::{len(raised)} baseline entries are not in {source}. "
            "Split the function instead of adding it to the baseline."
        )
        for line in raised:
            print(f"  + {line}")
    return bool(raised)


def _offenders(lines: list[str], violations: list[Violation]) -> list[str]:
    keys = {tuple(line.split(" ", 2)) for line in lines}
    return [
        f"    {f}:{row} {name}: {msg}"
        for f, row, rule, name, msg in violations
        if (f, rule, name) in keys
    ]


def main(
    argv: Sequence[str] | None = None,
    baseline_path: Path = REPO_ROOT / BASELINE_REL,
    measure_tree: Measure = measure,
    git: Runner = _git,
) -> int:
    parser = argparse.ArgumentParser(
        description="Hold ruff C901/PLR0915 to a shrink-only baseline of functions."
    )
    parser.add_argument(
        "--update", action="store_true", help="rewrite the baseline from this run"
    )
    parser.add_argument(
        "--allow-increase",
        action="store_true",
        help="with --update, also add new functions (the CI base check still flags them)",
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
    if not actual and not ruff_reports_both_rules(measure_tree):
        print("::error::ruff no longer reports C901/PLR0915, even on a canary")
        return 2

    try:
        baseline = parse_baseline(baseline_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"::error::{baseline_path.name} is not a baseline: {exc}")
        return 2
    above, below = compare(baseline, actual)

    if args.update:
        if above and not args.allow_increase:
            print("::error::Refusing to add functions to the baseline; split them:")
            for line in above:
                print(f"  + {line}")
            print(*_offenders(above, violations), sep="\n")
            return 1
        baseline_path.write_text(render_baseline(actual), encoding="utf-8")
        total = sum(_entries(actual).values())
        print(f"Wrote {baseline_path.name}: {total} over-limit function findings")
        return 0

    failed = False
    if args.base_sha is not None:
        failed = check_base(args.base_sha, baseline, git, measure_tree)
    print(
        f"C901/PLR0915 in {SOURCE_DIR}/: {sum(_entries(actual).values())} findings "
        f"(baseline {sum(_entries(baseline).values())})"
    )
    if above:
        print(
            f"::error::{len(above)} function(s) over C901 (complexity > "
            f"{MAX_COMPLEXITY}) or PLR0915 (> {MAX_STATEMENTS} statements) are "
            f"not in {BASELINE_REL}. Split them; do not add them to the baseline."
        )
        for line in above:
            print(f"  + {line}")
        print(*_offenders(above, violations), sep="\n")
    if below:
        print(
            f"::error::{len(below)} baseline function(s) are no longer over the "
            f"limit. Lock the improvement in: {UPDATE_COMMAND}"
        )
        for line in below:
            print(f"  - {line}")
    return 1 if above or below or failed else 0


if __name__ == "__main__":
    sys.exit(main())
