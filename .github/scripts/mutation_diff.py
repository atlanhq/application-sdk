"""Diff-scoped mutation testing driver (advisory).

Runs mutmut against only the mutants that live in Python files changed
vs. a base ref, then summarises which seeded faults the unit suite caught
(killed) and which it missed (survived). Wraps `mutmut run` / `mutmut
results` so the workflow `run:` block stays straight-line per
docs/standards/ci.md.

Usage (CI and local):
    python .github/scripts/mutation_diff.py --base-ref origin/main

Behaviour:
- No changed product files  -> writes a "nothing to mutate" summary, exit 0.
- Changed files but mutmut finds no mutants for them (e.g. only excluded
  subtrees or non-function code changed) -> "no mutants" summary, exit 0.
- Otherwise writes a scorecard (killed/survived/score + surviving mutant
  names) to $GITHUB_STEP_SUMMARY (when set) and stdout.
- Advisory by default: always exits 0 on a completed measurement. Pass
  --min-score N to turn the score into a gate (future graduation path).
- Infrastructure failures (mutmut crash for any other reason) exit non-zero.

Mutant-name mapping: mutmut names mutants
`<module.path>.<mangled_function>__mutmut_<n>`, so a changed file
`application_sdk/services/objectstore.py` maps to the fnmatch pattern
`application_sdk.services.objectstore.*` accepted by `mutmut run`.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from collections import Counter

SOURCE_ROOT = "application_sdk"

# Keep in sync with [tool.mutmut] do_not_mutate in pyproject.toml. Changed
# files under these subtrees are dropped before pattern derivation so an
# exclusions-only diff is reported as "nothing to mutate" instead of
# tripping mutmut's "nothing matches" assertion.
EXCLUDED_PREFIXES = (
    "application_sdk/test_utils/",
    "application_sdk/testing/scale_data_generator/",
    "application_sdk/testing/integration/",
    "application_sdk/testing/e2e/",
    "application_sdk/testing/hypothesis/",
    "application_sdk/testing/parity/",
)

# `mutmut results --all` statuses that mean the fault was detected.
DETECTED_STATUSES = frozenset({"killed", "timeout", "caught by type check", "segfault"})
MISSED_STATUS = "survived"
# Function has mutants but no test exercises it at all.
UNTESTED_STATUS = "no tests"

NOTHING_MATCHES_MARKER = "Filtered for specific mutants, but nothing matches"

# mutmut runs pytest in-process, so it must share an environment with the
# unit-test dependencies. Mirrors the setup-deps composite action's
# `uv sync --all-extras --all-groups`.
UV_RUN = ["uv", "run", "--all-extras", "--all-groups"]

# Cap the per-mutant gap list in the summary; a low-scoring large diff can
# have hundreds of survivors and GitHub truncates giant step summaries.
MAX_LISTED_GAPS = 40


def changed_python_files(base_ref: str) -> list[str]:
    """Product .py files changed vs. the merge-base with ``base_ref``."""
    out = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            f"{base_ref}...HEAD",
            "--",
            f"{SOURCE_ROOT}/**/*.py",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    files = [line.strip() for line in out.splitlines() if line.strip()]
    return [f for f in files if not f.startswith(EXCLUDED_PREFIXES)]


def module_patterns(files: list[str]) -> list[str]:
    """Map changed file paths to mutmut mutant-name fnmatch patterns."""
    patterns = []
    for f in files:
        module = f.removesuffix(".py").replace("/", ".")
        # Package changes mutate every module in the package.
        module = module.removesuffix(".__init__")
        patterns.append(f"{module}.*")
    return sorted(set(patterns))


def run_mutmut(patterns: list[str]) -> tuple[bool, str]:
    """Run diff-scoped mutation testing.

    Returns (measured, detail). ``measured`` is False when mutmut aborted
    because no mutant matched the patterns — a valid "nothing to measure"
    outcome, not an error. Any other non-zero exit is re-raised.
    """
    proc = subprocess.run(
        [*UV_RUN, "mutmut", "run", *patterns],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True, proc.stdout
    combined = proc.stdout + proc.stderr
    if NOTHING_MATCHES_MARKER in combined:
        return False, combined
    print(combined, file=sys.stderr)
    raise SystemExit(
        f"mutmut run failed with exit code {proc.returncode} "
        "(not a 'nothing matches' outcome)"
    )


def collect_results() -> str:
    # `--all` is a click option with a value (not a flag): include killed
    # mutants too, so the score denominator is complete.
    return subprocess.run(
        [*UV_RUN, "mutmut", "results", "--all", "true"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def parse_results(results_output: str, patterns: list[str]) -> dict[str, str]:
    """Parse `mutmut results --all` lines into {mutant_name: status}.

    Only mutants matching one of ``patterns`` are kept (results lists the
    whole cached run, not just this invocation's subset).
    """
    status_by_mutant: dict[str, str] = {}
    for line in results_output.splitlines():
        line = line.strip()
        if not line or ": " not in line:
            continue
        name, status = line.rsplit(": ", 1)
        if any(fnmatch.fnmatch(name, p) for p in patterns):
            status_by_mutant[name] = status
    return status_by_mutant


def score(status_by_mutant: dict[str, str]) -> tuple[float | None, Counter]:
    """Mutation score over decided mutants: detected / (detected + survived).

    "no tests" mutants are excluded from the score (they indicate missing
    coverage, reported separately) as are transient states like "not checked".
    Returns (score or None when nothing was decided, per-status counts).
    """
    counts = Counter(status_by_mutant.values())
    detected = sum(counts[s] for s in DETECTED_STATUSES)
    missed = counts[MISSED_STATUS]
    decided = detected + missed
    return (detected / decided if decided else None), counts


def render_summary(
    files: list[str],
    status_by_mutant: dict[str, str],
    mutation_score: float | None,
    counts: Counter,
) -> str:
    lines = ["## Mutation testing (diff-scoped, advisory)", ""]
    if not files:
        lines.append("No product code changed — nothing to mutate.")
        return "\n".join(lines) + "\n"
    lines.append(f"Changed product files: **{len(files)}**")
    lines.append("")
    if not status_by_mutant:
        lines.append(
            "mutmut generated no mutants for the changed lines "
            "(non-function code, or excluded subtrees)."
        )
        return "\n".join(lines) + "\n"

    detected = sum(counts[s] for s in DETECTED_STATUSES)
    survived = counts[MISSED_STATUS]
    untested = counts[UNTESTED_STATUS]
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    if mutation_score is not None:
        lines.append(f"| **Mutation score** | **{mutation_score:.0%}** |")
    lines.append(f"| Mutants detected (killed/timeout/type-check) | {detected} |")
    lines.append(f"| Mutants survived (bug would ship silently) | {survived} |")
    if untested:
        lines.append(f"| Mutants in functions no test exercises | {untested} |")
    other = sum(counts.values()) - detected - survived - untested
    if other:
        lines.append(f"| Other (skipped/suspicious/not checked) | {other} |")
    lines.append("")

    interesting = {
        name: status
        for name, status in sorted(status_by_mutant.items())
        if status in (MISSED_STATUS, UNTESTED_STATUS)
    }
    if interesting:
        lines.append("### Gaps — seeded bugs the unit suite does not catch")
        lines.append("")
        for name, status in list(interesting.items())[:MAX_LISTED_GAPS]:
            lines.append(f"- `{name}` — {status}")
        if len(interesting) > MAX_LISTED_GAPS:
            lines.append(f"- …and {len(interesting) - MAX_LISTED_GAPS} more")
        lines.append("")
        lines.append(
            "Inspect locally with `uv run --group mutation mutmut show "
            "<mutant-name>` (shows the exact code change that went undetected)."
        )
    return "\n".join(lines) + "\n"


def write_summary(markdown: str) -> None:
    print(markdown)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a") as fh:
            fh.write(markdown)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Base ref to diff against (default: origin/main)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="Fail (exit 1) when the mutation score is below this fraction "
        "(e.g. 0.6). Omitted = advisory, always exit 0.",
    )
    args = parser.parse_args(argv)

    files = changed_python_files(args.base_ref)
    if not files:
        write_summary(render_summary([], {}, None, Counter()))
        return 0

    patterns = module_patterns(files)
    measured, _ = run_mutmut(patterns)
    status_by_mutant = parse_results(collect_results(), patterns) if measured else {}
    mutation_score, counts = score(status_by_mutant)
    write_summary(render_summary(files, status_by_mutant, mutation_score, counts))

    if (
        args.min_score is not None
        and mutation_score is not None
        and mutation_score < args.min_score
    ):
        print(
            f"Mutation score {mutation_score:.0%} is below the required "
            f"{args.min_score:.0%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
