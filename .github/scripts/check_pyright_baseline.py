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

Comparing against the PR's own baseline is not enough on its own: a PR could add
a finding and raise its baseline (``--update``) in the same change, and both
counts would agree. So, given ``--base-sha``, the PR's baseline is also compared
with the base revision's, and any (file, rule) count that RISES fails. Two
cases are handled explicitly rather than refused:

* a file renamed in the PR carries its base counts to the new path (git's
  rename detection), so moving a script does not read as new findings;
* a PR that changes the pinned pyright version (recorded in the baseline as
  ``pyright_version``) may raise counts, because a new pyright finds new things
  in unchanged code. The increases are printed as warnings for the reviewer.

A base revision with no baseline file is the PR that introduces this check.
Every other failure to read the base (no SHA, a failed fetch) fails the check.

A run in which pyright analyses zero files fails rather than passes: that is
exactly how the excluded scripts went unchecked, and an empty run would match an
empty baseline.

Usage:
    python .github/scripts/check_pyright_baseline.py            # check (local)
    python .github/scripts/check_pyright_baseline.py --base-sha SHA   # check (CI)
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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ".github/scripts"
BASELINE = REPO_ROOT / SCRIPTS_DIR / "pyright_baseline.json"
_PYRIGHT_PIN_RE = re.compile(r"^pyright==(?P<version>\S+)$")
#: A stalled fetch of the base must not hold the job until its timeout.
GIT_TIMEOUT_S = 120
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


class BaseRevisionError(RuntimeError):
    """The base revision's baseline could not be read."""


@dataclass(frozen=True)
class Baseline:
    """A parsed ``pyright_baseline.json``."""

    pyright_version: str | None
    files: Counts


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=PYRIGHT_TIMEOUT_S,
        check=False,
    )


def _git(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *cmd],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git", *cmd], 124, "", "timed out")


def pyright_pin(pyproject: Path) -> str:
    """The exact ``pyright==`` version in the ``dev`` dependency group."""
    groups = tomllib.loads(pyproject.read_text(encoding="utf-8"))["dependency-groups"]
    for dep in groups["dev"]:
        match = _PYRIGHT_PIN_RE.match(dep)
        if match:
            return match.group("version")
    raise PyrightRunError(f"{pyproject.name} has no exact `pyright==` dev pin")


def build_config(pyproject: Path, repo_root: Path) -> dict[str, object]:
    """The repo's ``[tool.pyright]`` settings, scoped to ``.github/scripts``.

    Every setting is kept (rule levels, type-checking mode, the venv) except
    ``include``/``exclude``. The config is written INSIDE ``.github/scripts``
    and includes ``"."``: pyright (1.1.414) auto-excludes ``**/.*`` whatever
    ``exclude`` says, and matches it against paths below the config's
    directory, so a config at the repo root can never reach ``.github/``. It
    also ignores an absolute ``include``. The repo-relative ``venvPath`` and
    ``extraPaths`` are made absolute so they keep their repo-root meaning.
    """
    settings = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    config: dict[str, object] = dict(settings["tool"]["pyright"])
    config["include"] = ["."]
    config["exclude"] = ["**/__pycache__"]
    venv_path = config.get("venvPath")
    if isinstance(venv_path, str):
        config["venvPath"] = str((repo_root / venv_path).resolve())
    extra_paths = config.get("extraPaths")
    if isinstance(extra_paths, list):
        config["extraPaths"] = [str((repo_root / str(e)).resolve()) for e in extra_paths]
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
    config = build_config(repo_root / "pyproject.toml", repo_root)
    fd, name = tempfile.mkstemp(
        prefix=".pyright-scripts-", suffix=".json", dir=repo_root / SCRIPTS_DIR
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


def render_baseline(counts: Counts, pyright_version: str) -> str:
    doc = {
        "_comment": (
            "Generated by .github/scripts/check_pyright_baseline.py --update. "
            "Counts may only go down; see docs/standards/ci.md."
        ),
        "pyright_version": pyright_version,
        "totals": totals(counts),
        "files": counts,
    }
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def parse_baseline(text: str) -> Baseline:
    """Parse and validate a baseline document; ValueError if it is not one."""
    loaded: object = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError("baseline is not a JSON object")
    doc = cast("dict[str, object]", loaded)
    raw_files = doc.get("files")
    if not isinstance(raw_files, dict):
        raise ValueError("baseline has no `files` mapping")
    files: Counts = {}
    for file, raw_rules in cast("dict[str, object]", raw_files).items():
        if not isinstance(raw_rules, dict):
            raise ValueError(f"baseline entry for {file!r} is not a rule -> count map")
        per_rule = cast("dict[object, object]", raw_rules)
        if not all(isinstance(k, str) and isinstance(n, int) for k, n in per_rule.items()):
            raise ValueError(f"baseline entry for {file!r} is not a rule -> count map")
        files[file] = cast("dict[str, int]", per_rule)
    version = doc.get("pyright_version")
    return Baseline(version if isinstance(version, str) else None, files)


def load_baseline(path: Path) -> Baseline:
    return parse_baseline(path.read_text(encoding="utf-8"))


def read_base_baseline(
    base_sha: str, baseline_rel: str, git: Runner = _git
) -> Baseline | None:
    """The base revision's baseline, or None if the base has none.

    Fails closed: only a base commit that is present and provably lacks the file
    returns None. A missing SHA or a failed fetch raises.
    """
    if not base_sha:
        raise BaseRevisionError("--base-sha was given but is empty")
    fetch = git(["fetch", "--no-tags", "--depth=1", "origin", base_sha])
    if fetch.returncode != 0:
        raise BaseRevisionError(f"could not fetch base {base_sha}: {fetch.stderr.strip()}")
    if git(["cat-file", "-e", f"{base_sha}^{{commit}}"]).returncode != 0:
        raise BaseRevisionError(f"base {base_sha} is not a commit in this checkout")
    # ``cat-file -e <sha>:<path>`` cannot tell "no such path" from "tree
    # unreadable": both exit non-zero. ``ls-tree`` can: it exits 0 with no
    # output only when the base's (readable) tree has no entry at the path.
    listing = git(["ls-tree", "--full-tree", base_sha, "--", baseline_rel])
    if listing.returncode != 0:
        raise BaseRevisionError(
            f"could not list {baseline_rel} at {base_sha}: {listing.stderr.strip()}"
        )
    if not listing.stdout.strip():
        return None
    show = git(["show", f"{base_sha}:{baseline_rel}"])
    if show.returncode != 0:
        raise BaseRevisionError(f"could not read {baseline_rel} at {base_sha}")
    try:
        return parse_baseline(show.stdout)
    except ValueError as exc:
        raise BaseRevisionError(f"{baseline_rel} at {base_sha} is not a baseline: {exc}") from exc


def renamed_paths(base_sha: str, git: Runner = _git) -> dict[str, str]:
    """``{base path: head path}`` for files under SCRIPTS_DIR renamed since base."""
    diff = git(["diff", "--name-status", "-M", base_sha, "HEAD", "--", SCRIPTS_DIR])
    if diff.returncode != 0:
        raise BaseRevisionError(f"could not diff against base {base_sha}")
    renames: dict[str, str] = {}
    for line in diff.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            renames[parts[1]] = parts[2]
    return renames


def raised_since_base(
    base: Mapping[str, Mapping[str, int]],
    head: Mapping[str, Mapping[str, int]],
    renames: Mapping[str, str],
) -> list[str]:
    """(file, rule) counts the PR's baseline raises above the base's."""
    carried: dict[str, Counter[str]] = {}
    for file, per_rule in base.items():
        carried.setdefault(renames.get(file, file), Counter()).update(per_rule)
    increases, _ = compare(carried, head)
    return increases


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
    git: Runner = _git,
) -> int:
    parser = argparse.ArgumentParser(
        description="Hold pyright findings on .github/scripts to a shrink-only baseline."
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
        pin = pyright_pin(repo_root / "pyproject.toml")
        actual = run_pyright(repo_root, runner)
    except PyrightRunError as exc:
        print(f"::error::{exc}")
        return 2

    if args.update:
        baseline_path.write_text(render_baseline(actual, pin), encoding="utf-8")
        t = totals(actual)
        print(
            f"Wrote {baseline_path.name}: {t['error']} errors, {t['warning']} warnings"
        )
        return 0

    head = load_baseline(baseline_path)
    baseline = head.files
    failed = False
    if head.pyright_version != pin:
        print(
            f"::error::{baseline_path.name} was generated with pyright "
            f"{head.pyright_version}, but the dev pin is {pin}. Regenerate it: "
            f"python {SCRIPTS_DIR}/check_pyright_baseline.py --update"
        )
        failed = True
    if args.base_sha is not None:
        failed = _check_base(args.base_sha, head, baseline_path, repo_root, git) or failed
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
    return 1 if regressions or stale or failed else 0


def _check_base(
    base_sha: str,
    head: Baseline,
    baseline_path: Path,
    repo_root: Path,
    git: Runner,
) -> bool:
    """Print and return whether the PR's baseline rises above the base's."""
    baseline_rel = baseline_path.relative_to(repo_root).as_posix()
    try:
        base_doc = read_base_baseline(base_sha, baseline_rel, git)
        if base_doc is None:
            print(f"No {baseline_rel} at base {base_sha}: this PR introduces it.")
            return False
        renames = renamed_paths(base_sha, git)
    except BaseRevisionError as exc:
        print(f"::error::{exc}")
        return True
    increases = raised_since_base(base_doc.files, head.files, renames)
    if not increases:
        return False
    if base_doc.pyright_version != head.pyright_version:
        print(
            f"::warning::pyright changed {base_doc.pyright_version} -> {head.pyright_version}; "
            f"{len(increases)} baseline count(s) rose with it. Review them:"
        )
        for line in increases:
            print(f"  ! {line}")
        return False
    print(
        f"::error::{len(increases)} baseline count(s) rose above the base revision's "
        f"{baseline_rel}. The baseline may only shrink: fix the findings instead."
    )
    for line in increases:
        print(f"  + {line}")
    return True


if __name__ == "__main__":
    sys.exit(main())
