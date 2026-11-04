#!/usr/bin/env python3
"""Hold pyright's findings on ``.github/scripts`` to a baseline that only shrinks.

Why this exists (FND-4245)
--------------------------
``[tool.pyright]`` in ``pyproject.toml`` excludes ``.github/**``, and an exclude
wins even over paths passed on the command line, so the pre-commit pyright hook
analyses none of the CI scripts. Those scripts carry the logic for our
automations (docs/standards/ci.md), and the findings pyright has on them today
are mostly real (``reportOptionalMemberAccess`` and friends). Fixing all of them
in one change is not the point; stopping new ones is.

So this check runs pyright over ``.github/scripts`` with the repo's own pyright
settings, minus the exclude, and compares the result per file and per rule with
``pyright_baseline.json``:

* a (file, rule) count ABOVE its baseline fails: a new finding;
* a (file, rule) count BELOW its baseline also fails, asking for ``--update``.
  A baseline left looser than reality has slack in it, and a later regression in
  the same file would hide inside that slack. Committing the lower count is what
  makes the baseline ratchet.

Counting per (file, rule), not per file, means that fixing one finding does not
buy permission for an unrelated new one in the same file.

A run in which pyright analyses zero files fails rather than passes: that is
exactly how the excluded scripts went unchecked, and an empty run would match an
empty baseline.

Usage:
    python .github/scripts/check_pyright_baseline.py            # check
    python .github/scripts/check_pyright_baseline.py --update   # rewrite baseline

Environment:
    GITHUB_STEP_SUMMARY   when set, the totals are appended to the job summary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from pathlib import Path
from typing import Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ".github/scripts"
BASELINE = REPO_ROOT / SCRIPTS_DIR / "pyright_baseline.json"
#: Severities the baseline tracks. ``information`` is not a finding.
TRACKED_SEVERITIES = ("error", "warning")
#: pyright exits 0 (clean) or 1 (diagnostics reported); anything else is fatal.
PYRIGHT_OK_EXIT_CODES = (0, 1)
#: pyright on ~360 files takes about a minute; a hang must not hold the runner.
PYRIGHT_TIMEOUT_S = 600

#: Per file, ``"<severity>:<rule>"`` -> count.
Counts = dict[str, dict[str, int]]
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class PyrightRunError(RuntimeError):
    """pyright did not produce a usable report."""


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=PYRIGHT_TIMEOUT_S,
        check=False,
    )


def build_config(pyproject: Path) -> dict[str, object]:
    """The repo's ``[tool.pyright]`` settings, scoped to ``.github/scripts``.

    Every setting is kept (rule levels, type-checking mode, the venv) except
    ``include``/``exclude``. The config is written at the repo root because
    pyright resolves every path in it (and ignores an absolute ``include``)
    relative to the config file's directory, so the root keeps the repo's
    relative ``venvPath``/``extraPaths`` meaning what they mean there.
    """
    settings = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    config: dict[str, object] = dict(settings["tool"]["pyright"])
    config["include"] = [SCRIPTS_DIR]
    # Replacing exclude also drops pyright's default ``**/.*``, which would
    # otherwise match the ``.github`` directory itself.
    config["exclude"] = ["**/__pycache__"]
    # Pinned, not inferred from the interpreter that runs this, so a baseline
    # regenerated on a laptop matches the Linux CI runner. The version is the
    # requires-python floor: the oldest Python the scripts must stay valid on.
    config["pythonPlatform"] = "Linux"
    config["pythonVersion"] = requires_python_floor(
        settings["project"]["requires-python"]
    )
    return config


def requires_python_floor(spec: str) -> str:
    """``">=3.11"`` -> ``"3.11"``; anything else is a spec this cannot pin."""
    match = re.fullmatch(
        r">=\s*(\d+\.\d+)(?:\.\d+)?(?:\s*,\s*<\s*[\d.]+)?", spec.strip()
    )
    if match is None:
        raise PyrightRunError(
            f"cannot derive a pythonVersion from requires-python {spec!r}"
        )
    return match.group(1)


def parse_report(stdout: str, repo_root: Path) -> tuple[Counts, int]:
    """Count tracked diagnostics per (file, severity:rule); return files analysed."""
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PyrightRunError(f"pyright did not print a JSON report: {exc}") from exc
    counts: dict[str, Counter[str]] = {}
    for diag in report.get("generalDiagnostics", []):
        severity = diag.get("severity")
        if severity not in TRACKED_SEVERITIES:
            continue
        path = Path(diag["file"])
        try:
            rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        key = f"{severity}:{diag.get('rule') or 'unruled'}"
        counts.setdefault(rel, Counter())[key] += 1
    files_analysed = int(report.get("summary", {}).get("filesAnalyzed", 0))
    return (
        {f: dict(sorted(c.items())) for f, c in sorted(counts.items())},
        files_analysed,
    )


def run_pyright(repo_root: Path, runner: Runner = _run) -> Counts:
    config = build_config(repo_root / "pyproject.toml")
    fd, name = tempfile.mkstemp(
        prefix=".pyright-scripts-", suffix=".json", dir=repo_root
    )
    config_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        result = runner(["pyright", "--outputjson", "--project", str(config_path)])
    finally:
        config_path.unlink(missing_ok=True)
    if result.returncode not in PYRIGHT_OK_EXIT_CODES:
        raise PyrightRunError(
            f"pyright exited {result.returncode}: {result.stderr.strip() or result.stdout[:2000]}"
        )
    counts, files_analysed = parse_report(result.stdout, repo_root)
    if files_analysed == 0:
        raise PyrightRunError(
            f"pyright analysed 0 files under {SCRIPTS_DIR}; the scope is broken, "
            "and an empty run would match an empty baseline"
        )
    return counts


def totals(counts: Mapping[str, Mapping[str, int]]) -> dict[str, int]:
    out = {severity: 0 for severity in TRACKED_SEVERITIES}
    for per_rule in counts.values():
        for key, n in per_rule.items():
            out[key.split(":", 1)[0]] += n
    return out


def compare(
    baseline: Mapping[str, Mapping[str, int]], actual: Mapping[str, Mapping[str, int]]
) -> tuple[list[str], list[str]]:
    """Return (regressions, stale entries), each a sorted list of messages."""
    regressions: list[str] = []
    stale: list[str] = []
    for file in sorted(set(baseline) | set(actual)):
        base_rules = baseline.get(file, {})
        actual_rules = actual.get(file, {})
        for key in sorted(set(base_rules) | set(actual_rules)):
            was, now = base_rules.get(key, 0), actual_rules.get(key, 0)
            if now > was:
                regressions.append(f"{file}: {key} {was} -> {now}")
            elif now < was:
                stale.append(f"{file}: {key} {was} -> {now}")
    return regressions, stale


def render_baseline(counts: Counts) -> str:
    doc = {
        "_comment": (
            "Generated by .github/scripts/check_pyright_baseline.py --update. "
            "Counts may only go down; see docs/standards/ci.md."
        ),
        "totals": totals(counts),
        "files": counts,
    }
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def load_baseline(path: Path) -> Counts:
    doc = json.loads(path.read_text(encoding="utf-8"))
    files: Counts = doc["files"]
    return files


def _write_summary(actual: Counts, baseline: Counts) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    now, was = totals(actual), totals(baseline)
    lines = [
        "### pyright on `.github/scripts`",
        "",
        "| severity | baseline | this run |",
        "| --- | ---: | ---: |",
        *(f"| {s} | {was[s]} | {now[s]} |" for s in TRACKED_SEVERITIES),
        "",
    ]
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main(
    argv: Sequence[str] | None = None,
    runner: Runner = _run,
    repo_root: Path = REPO_ROOT,
    baseline_path: Path = BASELINE,
) -> int:
    parser = argparse.ArgumentParser(
        description="Hold pyright findings on .github/scripts to a shrink-only baseline."
    )
    parser.add_argument(
        "--update", action="store_true", help="rewrite the baseline from this run"
    )
    args = parser.parse_args(argv)

    try:
        actual = run_pyright(repo_root, runner)
    except PyrightRunError as exc:
        print(f"::error::{exc}")
        return 2

    if args.update:
        baseline_path.write_text(render_baseline(actual), encoding="utf-8")
        t = totals(actual)
        print(
            f"Wrote {baseline_path.name}: {t['error']} errors, {t['warning']} warnings"
        )
        return 0

    baseline = load_baseline(baseline_path)
    _write_summary(actual, baseline)
    regressions, stale = compare(baseline, actual)
    t = totals(actual)
    print(f"pyright on {SCRIPTS_DIR}: {t['error']} errors, {t['warning']} warnings")
    if regressions:
        print(
            f"::error::{len(regressions)} pyright finding count(s) rose above the "
            "baseline. Fix the new findings; do not raise the baseline."
        )
        for line in regressions:
            print(f"  + {line}")
    if stale:
        print(
            f"::error::{len(stale)} pyright finding count(s) fell below the baseline. "
            f"Lock the improvement in: python {SCRIPTS_DIR}/check_pyright_baseline.py "
            "--update, and commit the result."
        )
        for line in stale:
            print(f"  - {line}")
    return 1 if regressions or stale else 0


if __name__ == "__main__":
    sys.exit(main())
