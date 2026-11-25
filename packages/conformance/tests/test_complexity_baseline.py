"""Function size and complexity in this package: a baseline that only shrinks (FND-4668).

ruff's C901 (McCabe complexity above ``max-complexity``) and PLR0915 (more
statements than ``max-statements``) are measured over ``conformance/``, with the
thresholds pinned in this package's ``[tool.ruff.lint.mccabe]`` /
``[tool.ruff.lint.pylint]``. They are not in the ruff ``select`` list: ~190
existing functions exceed them, and a select would turn every one into a red
pre-commit. Instead ``ruff_complexity_baseline.json`` records the count per
(file, rule), and:

* **a count above the baseline fails** — a new over-limit function, or an
  existing one grown past a threshold in a file that already had one. Split it.
* **a count below the baseline fails** — something got paid down; lock it in
  with ``--update``.
* **with ``--base-sha``, a baseline count above the base revision's fails** —
  so raising the baseline to make room for a new function does not get past CI.
  ``--update`` itself refuses to raise a count unless ``--allow-increase`` is
  passed, and the base check still catches that in CI.

Run as a script (pre-commit and CI do; it needs only ruff and the stdlib)::

    uv run --frozen python packages/conformance/tests/test_complexity_baseline.py
    uv run --frozen python packages/conformance/tests/test_complexity_baseline.py --update
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from functools import cache
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
BASELINE = PACKAGE_DIR / "ruff_complexity_baseline.json"
SOURCE_DIR = "conformance"
RULES = ("C901", "PLR0915")
#: The thresholds the baseline was measured at. Raising one would empty the
#: baseline without splitting anything, so they are pinned here too.
MAX_COMPLEXITY = 10
MAX_STATEMENTS = 50

Counts = dict[str, dict[str, int]]
Violation = tuple[str, str, int, str]  # (file, rule, line, ruff message)


def _ruff_cmd() -> list[str]:
    exe = shutil.which("ruff")
    return [exe] if exe else [sys.executable, "-m", "ruff"]


@cache
def measure() -> tuple[Counts, tuple[Violation, ...]]:
    """Run ruff over the package source and count C901/PLR0915 per file."""
    proc = subprocess.run(
        [
            *_ruff_cmd(),
            "check",
            "--no-cache",
            "--exit-zero",
            "--output-format=json",
            f"--select={','.join(RULES)}",
            SOURCE_DIR,
        ],
        cwd=PACKAGE_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    violations: list[Violation] = []
    for item in json.loads(proc.stdout):
        if item["code"] not in RULES:
            continue
        rel = Path(item["filename"]).resolve().relative_to(PACKAGE_DIR).as_posix()
        violations.append((rel, item["code"], item["location"]["row"], item["message"]))
    tally = Counter((file, rule) for file, rule, _, _ in violations)
    counts: Counts = {}
    for (file, rule), n in sorted(tally.items()):
        counts.setdefault(file, {})[rule] = n
    return counts, tuple(sorted(violations))


def load_baseline(text: str | None = None) -> Counts:
    data = json.loads(BASELINE.read_text() if text is None else text)
    return {f: {r: int(n) for r, n in rules.items()} for f, rules in data.items()}


def _flat(counts: Counts) -> dict[tuple[str, str], int]:
    return {(f, r): n for f, rules in counts.items() for r, n in rules.items()}


def compare(measured: Counts, baseline: Counts) -> tuple[list[str], list[str]]:
    """Return (over-baseline messages, under-baseline messages)."""
    got, want = _flat(measured), _flat(baseline)
    over, under = [], []
    for key in sorted(got.keys() | want.keys()):
        n, b = got.get(key, 0), want.get(key, 0)
        if n > b:
            over.append(f"{key[0]} {key[1]}: {n} > baseline {b}")
        elif n < b:
            under.append(f"{key[0]} {key[1]}: {n} < baseline {b}")
    return over, under


def raised_over(baseline: Counts, base: Counts) -> list[str]:
    """Counts *baseline* sets higher than *base* did."""
    now, before = _flat(baseline), _flat(base)
    return [
        f"{f} {r}: baseline {n} > base revision's {before.get((f, r), 0)}"
        for (f, r), n in sorted(now.items())
        if n > before.get((f, r), 0)
    ]


def _base_baseline(sha: str) -> Counts | None:
    """The baseline at *sha*, fetching the commit into a shallow clone if needed."""
    rel = BASELINE.relative_to(_git_root()).as_posix()
    if subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=PACKAGE_DIR,
        capture_output=True,
    ).returncode:
        subprocess.run(
            ["git", "fetch", "--no-tags", "--depth=1", "origin", sha],
            cwd=PACKAGE_DIR,
            check=True,
        )
    show = subprocess.run(
        ["git", "show", f"{sha}:{rel}"], cwd=PACKAGE_DIR, capture_output=True, text=True
    )
    # The base predates the baseline: every count is new, so all are "raised".
    return load_baseline(show.stdout) if show.returncode == 0 else None


def _git_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=PACKAGE_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip()).resolve()


def _offenders(over: list[str]) -> list[str]:
    _, violations = measure()
    files = {line.split(" ", 1)[0] for line in over}
    return [
        f"  {f}:{line} {rule} {msg}" for f, rule, line, msg in violations if f in files
    ]


# ── pytest ─────────────────────────────────────────────────────────────────


def test_no_count_exceeds_the_baseline() -> None:
    over, _ = compare(measure()[0], load_baseline())
    assert not over, (
        "Function size/complexity above ruff_complexity_baseline.json — split the "
        "new or grown function (C901 > {} branches, PLR0915 > {} statements):\n{}\n"
        "Over-limit functions in those files:\n{}".format(
            MAX_COMPLEXITY, MAX_STATEMENTS, "\n".join(over), "\n".join(_offenders(over))
        )
    )


def test_baseline_has_no_slack() -> None:
    _, under = compare(measure()[0], load_baseline())
    assert not under, (
        "Fewer over-limit functions than ruff_complexity_baseline.json allows. "
        "Lock the improvement in:\n  uv run --frozen python "
        "packages/conformance/tests/test_complexity_baseline.py --update\n"
        + "\n".join(under)
    )


def test_thresholds_are_pinned_in_the_ruff_config() -> None:
    lint = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text())["tool"]["ruff"][
        "lint"
    ]
    assert lint["mccabe"]["max-complexity"] == MAX_COMPLEXITY
    assert lint["pylint"]["max-statements"] == MAX_STATEMENTS


def test_ruff_measures_something() -> None:
    # A ruff that silently analysed nothing would "match" an emptied baseline.
    assert measure()[0], "ruff reported no C901/PLR0915 findings at all"


def test_baseline_is_canonically_written() -> None:
    assert BASELINE.read_text() == _dump(load_baseline())


def test_raised_over_flags_new_and_grown_counts() -> None:
    base = {"a.py": {"C901": 2}}
    assert raised_over({"a.py": {"C901": 2}}, base) == []
    assert raised_over({"a.py": {"C901": 1}}, base) == []
    assert raised_over({"a.py": {"C901": 3}}, base) == [
        "a.py C901: baseline 3 > base revision's 2"
    ]
    assert raised_over({"b.py": {"PLR0915": 1}}, base) == [
        "b.py PLR0915: baseline 1 > base revision's 0"
    ]


# ── CLI ────────────────────────────────────────────────────────────────────


def _dump(counts: Counts) -> str:
    return json.dumps(counts, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hold C901/PLR0915 counts to ruff_complexity_baseline.json."
    )
    parser.add_argument("--update", action="store_true", help="rewrite the baseline")
    parser.add_argument(
        "--allow-increase",
        action="store_true",
        help="with --update, let a count rise (the CI base check still flags it)",
    )
    parser.add_argument(
        "--base-sha",
        default="",
        help="also fail if the baseline raises a count over this revision's",
    )
    args = parser.parse_args(argv)
    measured, _ = measure()
    if not measured:
        print("ruff reported no C901/PLR0915 findings at all", file=sys.stderr)
        return 1
    baseline = load_baseline()
    over, under = compare(measured, baseline)

    if args.update:
        if over and not args.allow_increase:
            print(
                "Refusing to raise the baseline:",
                *over,
                *_offenders(over),
                sep="\n",
                file=sys.stderr,
            )
            return 1
        BASELINE.write_text(_dump(measured))
        print(f"wrote {BASELINE.name}")
        return 0

    failed = False
    if over:
        failed = True
        print(
            "Above the complexity baseline — split the function:",
            *over,
            *_offenders(over),
            sep="\n",
            file=sys.stderr,
        )
    if under:
        failed = True
        print(
            "Below the complexity baseline — run with --update:",
            *under,
            sep="\n",
            file=sys.stderr,
        )
    if args.base_sha:
        base = _base_baseline(args.base_sha)
        raised = raised_over(baseline, base) if base is not None else []
        if raised:
            failed = True
            print(
                f"Baseline raised over {args.base_sha}:",
                *raised,
                sep="\n",
                file=sys.stderr,
            )
    if not failed:
        print("complexity baseline: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
