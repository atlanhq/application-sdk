#!/usr/bin/env python3
"""Hold pyright's findings to a baseline that only shrinks, in two scopes.

``--scope scripts`` (the default) covers ``.github/scripts``; ``--scope sdk``
covers everything else ``[tool.pyright]`` analyses (``application_sdk``,
``packages``, ``tools``). The mechanics below are the same for both.

Why the sdk scope exists (FND-4295)
-----------------------------------
``reportArgumentType``, ``reportAttributeAccessIssue`` and
``reportFunctionMemberAccess`` are errors in ``[tool.pyright]``. Hundreds of
existing findings of those rules predate the promotion, so a plain pyright run
fails. The sdk scope holds the tree's ERRORS (not its warnings, which stay
advisory) to the repo-root ``pyright_baseline.json``: an argument-type bug in
new code fails, and the existing ones may only be fixed, never added to. It
replaces the pre-commit ``pyright-python`` hook, which fails on any error.

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
from typing import AbstractSet, Callable, Mapping, Sequence, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ".github/scripts"
BASELINE = REPO_ROOT / SCRIPTS_DIR / "pyright_baseline.json"
SDK_BASELINE = REPO_ROOT / "pyright_baseline.json"
#: Read when ``--base-sha`` is not given, so a pre-commit hook (whose arguments
#: are fixed) can still be held to the base revision's baseline in CI. Empty
#: (a push to main, a local run) means no base comparison.
BASE_SHA_ENV = "PYRIGHT_BASELINE_BASE_SHA"
_PYRIGHT_PIN_RE = re.compile(r"^pyright==(?P<version>\S+)$")
#: A stalled fetch of the base must not hold the job until its timeout.
GIT_TIMEOUT_S = 120
#: Severities the scripts baseline tracks. ``information`` is not a finding.
TRACKED_SEVERITIES = ("error", "warning")
#: pyright exits 0 (clean) or 1 (diagnostics reported); anything else is fatal.
PYRIGHT_OK_EXIT_CODES = (0, 1)
#: pyright on ~360 files takes about a minute; a hang must not hold the runner.
PYRIGHT_TIMEOUT_S = 600

#: Per file, ``"<severity>:<rule>"`` -> count.
Counts = dict[str, dict[str, int]]
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Scope:
    """What one baseline covers and how pyright is pointed at it."""

    name: str
    #: How the scope is named in messages and the job summary.
    label: str
    #: Repo-relative path of the committed baseline.
    baseline_rel: str
    #: Repo-relative directory the generated pyright config is written in.
    config_dir: str
    #: Severities counted. The sdk scope counts errors only: its warnings
    #: (``reportUnknownMemberType`` above all) are advisory, not a ratchet.
    severities: tuple[str, ...]
    #: Keep ``[tool.pyright]``'s include/exclude (True), or analyse exactly
    #: ``config_dir`` (False: the scripts, which that exclude hides).
    keep_scope: bool
    #: Path ``git diff`` looks for renames under; ``"."`` is the whole repo.
    rename_root: str

    def update_command(self) -> str:
        flag = "" if self.name == "scripts" else f" --scope {self.name}"
        return f"python {SCRIPTS_DIR}/check_pyright_baseline.py{flag} --update"


SCOPES: dict[str, Scope] = {
    "scripts": Scope(
        name="scripts",
        label=SCRIPTS_DIR,
        baseline_rel=f"{SCRIPTS_DIR}/pyright_baseline.json",
        config_dir=SCRIPTS_DIR,
        severities=TRACKED_SEVERITIES,
        keep_scope=False,
        rename_root=SCRIPTS_DIR,
    ),
    "sdk": Scope(
        name="sdk",
        label="the SDK tree",
        baseline_rel="pyright_baseline.json",
        config_dir=".",
        # Errors only, so the base baseline has no entry for a rule that was a
        # warning there: promoting one would read as new findings. The base
        # comparison asks `promoted_rules` instead (F-61b341).
        severities=("error",),
        keep_scope=True,
        rename_root=".",
    ),
}


def promoted_rules(
    base_settings: Mapping[str, object],
    head_settings: Mapping[str, object],
    severities: Sequence[str],
) -> set[str]:
    """Rules this PR's ``[tool.pyright]`` moves INTO the tracked severities.

    The base baseline cannot hold a rule its scope did not track, so a
    promoted rule's findings are new keys, not new findings. Only a rule the
    base config sets explicitly to an untracked level counts: one it leaves at
    pyright's default may already have been tracked there, so it stays strict.
    """
    return {
        rule
        for rule, level in head_settings.items()
        if rule.startswith("report")
        and _level(level) in severities
        and rule in base_settings
        and _level(base_settings[rule]) not in severities
    }


def _level(value: object) -> str:
    """A rule's configured level; pyright reads ``true``/``false`` as error/none."""
    if value is True:
        return "error"
    if value is False:
        return "none"
    return str(value)


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


def pyright_settings(pyproject: Path) -> dict[str, object]:
    """``[tool.pyright]`` from a pyproject on disk (empty when it has none)."""
    return _pyright_table(pyproject.read_text(encoding="utf-8"))


def _pyright_table(text: str) -> dict[str, object]:
    doc: dict[str, object] = tomllib.loads(text)
    tool = doc.get("tool")
    if not isinstance(tool, dict):
        return {}
    table = cast("dict[str, object]", tool).get("pyright")
    return dict(cast("dict[str, object]", table)) if isinstance(table, dict) else {}


def read_base_pyright_settings(base_sha: str, git: Runner = _git) -> dict[str, object]:
    """The base revision's ``[tool.pyright]``; fails closed if unreadable."""
    show = git(["show", f"{base_sha}:pyproject.toml"])
    if show.returncode != 0:
        raise BaseRevisionError(f"could not read pyproject.toml at {base_sha}")
    try:
        return _pyright_table(show.stdout)
    except tomllib.TOMLDecodeError as exc:
        raise BaseRevisionError(
            f"pyproject.toml at {base_sha} is not TOML: {exc}"
        ) from exc


def pyright_pin(pyproject: Path) -> str:
    """The exact ``pyright==`` version in the ``dev`` dependency group."""
    groups = tomllib.loads(pyproject.read_text(encoding="utf-8"))["dependency-groups"]
    for dep in groups["dev"]:
        match = _PYRIGHT_PIN_RE.match(dep)
        if match:
            return match.group("version")
    raise PyrightRunError(f"{pyproject.name} has no exact `pyright==` dev pin")


def build_config(
    pyproject: Path, repo_root: Path, scope: Scope = SCOPES["scripts"]
) -> dict[str, object]:
    """The repo's ``[tool.pyright]`` settings, for one scope.

    Every setting is kept (rule levels, type-checking mode, the venv). The sdk
    scope also keeps ``include``/``exclude``; the scripts scope replaces them.
    The scripts config is written INSIDE ``.github/scripts``
    and includes ``"."``: pyright (1.1.414) auto-excludes ``**/.*`` whatever
    ``exclude`` says, and matches it against paths below the config's
    directory, so a config at the repo root can never reach ``.github/``. It
    also ignores an absolute ``include``. The repo-relative ``venvPath`` and
    ``extraPaths`` are made absolute so they keep their repo-root meaning.
    """
    settings = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    config: dict[str, object] = dict(settings["tool"]["pyright"])
    if not scope.keep_scope:
        config["include"] = ["."]
        config["exclude"] = ["**/__pycache__"]
    venv_path = config.get("venvPath")
    if isinstance(venv_path, str):
        config["venvPath"] = str((repo_root / venv_path).resolve())
    extra_paths = config.get("extraPaths")
    if isinstance(extra_paths, list):
        config["extraPaths"] = [
            str((repo_root / str(e)).resolve()) for e in extra_paths
        ]
    # Pinned, not inferred from the interpreter that runs this, so a baseline
    # regenerated on a laptop matches the Linux CI runner (a 3.14 venv's ast
    # stubs alone add errors a 3.13 runner does not see). The version is the
    # requires-python floor: the oldest Python the code must stay valid on.
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


def parse_report(
    stdout: str, repo_root: Path, severities: Sequence[str] = TRACKED_SEVERITIES
) -> tuple[Counts, int]:
    """Count tracked diagnostics per (file, severity:rule); return files analysed."""
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PyrightRunError(f"pyright did not print a JSON report: {exc}") from exc
    counts: dict[str, Counter[str]] = {}
    for diag in report.get("generalDiagnostics", []):
        severity = diag.get("severity")
        if severity not in severities:
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


def run_pyright(
    repo_root: Path, runner: Runner = _run, scope: Scope = SCOPES["scripts"]
) -> Counts:
    config = build_config(repo_root / "pyproject.toml", repo_root, scope)
    # A dot-file, so neither scope's pyright run analyses or lists it.
    fd, name = tempfile.mkstemp(
        prefix=f".pyright-{scope.name}-",
        suffix=".json",
        dir=repo_root / scope.config_dir,
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
    counts, files_analysed = parse_report(result.stdout, repo_root, scope.severities)
    if files_analysed == 0:
        raise PyrightRunError(
            f"pyright analysed 0 files in {scope.label}; the scope is broken, "
            "and an empty run would match an empty baseline"
        )
    return counts


def totals(
    counts: Mapping[str, Mapping[str, int]],
    severities: Sequence[str] = TRACKED_SEVERITIES,
) -> dict[str, int]:
    out = {severity: 0 for severity in severities}
    for per_rule in counts.values():
        for key, n in per_rule.items():
            severity = key.split(":", 1)[0]
            out[severity] = out.get(severity, 0) + n
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


def render_baseline(
    counts: Counts, pyright_version: str, scope: Scope = SCOPES["scripts"]
) -> str:
    doc = {
        "_comment": (
            f"Generated by {scope.update_command().removeprefix('python ')}. "
            "Counts may only go down; see docs/standards/ci.md."
        ),
        "pyright_version": pyright_version,
        "totals": totals(counts, scope.severities),
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
        if not all(
            isinstance(k, str) and isinstance(n, int) for k, n in per_rule.items()
        ):
            raise ValueError(f"baseline entry for {file!r} is not a rule -> count map")
        files[file] = cast("dict[str, int]", per_rule)
    version = doc.get("pyright_version")
    return Baseline(version if isinstance(version, str) else None, files)


def load_baseline(path: Path) -> Baseline:
    return parse_baseline(path.read_text(encoding="utf-8"))


def _fetch_depth(git: Runner) -> list[str]:
    """``--depth=1`` in a shallow checkout (CI), nothing in a full clone.

    ``fetch --depth=1 <sha>`` in a full clone records ``<sha>`` in
    ``.git/shallow`` as a history cut-off, which every worktree of the clone
    shares (FND-4987). Only a clone that is already shallow may be kept so.

    Fails closed: when git cannot say, neither fetch is safe (one may
    re-shallow a full clone, the other may pull a shallow CI checkout's whole
    history), so this raises instead of guessing.
    """
    probe = git(["rev-parse", "--is-shallow-repository"])
    state = probe.stdout.strip() if probe.returncode == 0 else ""
    if state == "true":
        return ["--depth=1"]
    if state == "false":
        return []
    raise BaseRevisionError(
        f"could not tell whether this checkout is shallow: {probe.stderr.strip()}"
    )


def read_base_baseline(
    base_sha: str, baseline_rel: str, git: Runner = _git
) -> Baseline | None:
    """The base revision's baseline, or None if the base has none.

    Fails closed: only a base commit that is present and provably lacks the file
    returns None. A missing SHA or a failed fetch raises.
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
        raise BaseRevisionError(
            f"{baseline_rel} at {base_sha} is not a baseline: {exc}"
        ) from exc


def renamed_paths(
    base_sha: str, git: Runner = _git, root: str = SCRIPTS_DIR
) -> dict[str, str]:
    """``{base path: head path}`` for files under ``root`` renamed since base."""
    diff = git(["diff", "--name-status", "-M", base_sha, "HEAD", "--", root])
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
    exempt_rules: AbstractSet[str] = frozenset(),
) -> list[str]:
    """(file, rule) counts the PR's baseline raises above the base's.

    Rules in ``exempt_rules`` (see ``promoted_rules``) are left out.

    Compared per rule, not per ``severity:rule``: a PR that promotes a rule
    from warning to error in ``[tool.pyright]`` moves its findings from one key
    to the other without adding any (FND-4295 did exactly that). Within one
    scope a rule has one severity, so summing over severities loses nothing.
    """
    carried: dict[str, Counter[str]] = {}
    for file, per_rule in base.items():
        carried.setdefault(renames.get(file, file), Counter()).update(
            _by_rule(per_rule, exempt_rules)
        )
    head_by_rule = {
        file: _by_rule(per_rule, exempt_rules) for file, per_rule in head.items()
    }
    increases, _ = compare(carried, head_by_rule)
    return increases


def _by_rule(
    per_key: Mapping[str, int], exempt_rules: AbstractSet[str] = frozenset()
) -> Counter[str]:
    """``{"severity:rule": n}`` -> ``{"rule": n}``, summed over severities."""
    out: Counter[str] = Counter()
    for key, n in per_key.items():
        if key.split(":", 1)[-1] in exempt_rules:
            continue
        out[key.split(":", 1)[-1]] += n
    return out


def _write_summary(actual: Counts, baseline: Counts, scope: Scope) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    now, was = totals(actual, scope.severities), totals(baseline, scope.severities)
    lines = [
        f"### pyright on {scope.label}",
        "",
        "| severity | baseline | this run |",
        "| --- | ---: | ---: |",
        *(f"| {s} | {was[s]} | {now[s]} |" for s in scope.severities),
        "",
    ]
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main(
    argv: Sequence[str] | None = None,
    runner: Runner = _run,
    repo_root: Path = REPO_ROOT,
    baseline_path: Path | None = None,
    git: Runner = _git,
) -> int:
    parser = argparse.ArgumentParser(
        description="Hold pyright findings to a shrink-only baseline."
    )
    parser.add_argument(
        "--scope",
        choices=sorted(SCOPES),
        default="scripts",
        help="scripts: .github/scripts (default); sdk: the rest of the repo",
    )
    parser.add_argument(
        "--update", action="store_true", help="rewrite the baseline from this run"
    )
    parser.add_argument(
        "--base-sha",
        help=(
            "base revision whose baseline this PR's baseline may not exceed "
            f"(default: ${BASE_SHA_ENV}, when set and non-empty)"
        ),
    )
    args = parser.parse_args(argv)
    scope = SCOPES[args.scope]
    if baseline_path is None:
        baseline_path = repo_root / scope.baseline_rel
    base_sha: str | None = args.base_sha
    if base_sha is None:
        base_sha = os.environ.get(BASE_SHA_ENV) or None

    try:
        pin = pyright_pin(repo_root / "pyproject.toml")
        actual = run_pyright(repo_root, runner, scope)
    except PyrightRunError as exc:
        print(f"::error::{exc}")
        return 2

    if args.update:
        baseline_path.write_text(render_baseline(actual, pin, scope), encoding="utf-8")
        t = totals(actual, scope.severities)
        print(
            f"Wrote {baseline_path.name}: "
            + ", ".join(f"{t[s]} {s}s" for s in scope.severities)
        )
        return 0

    head = load_baseline(baseline_path)
    baseline = head.files
    failed = False
    if head.pyright_version != pin:
        print(
            f"::error::{baseline_path.name} was generated with pyright "
            f"{head.pyright_version}, but the dev pin is {pin}. Regenerate it: "
            f"{scope.update_command()}"
        )
        failed = True
    if base_sha is not None:
        failed = (
            _check_base(base_sha, head, baseline_path, repo_root, git, scope) or failed
        )
    _write_summary(actual, baseline, scope)
    regressions, stale = compare(baseline, actual)
    t = totals(actual, scope.severities)
    print(
        f"pyright on {scope.label}: "
        + ", ".join(f"{t[s]} {s}s" for s in scope.severities)
    )
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
            f"Lock the improvement in: {scope.update_command()}, "
            "and commit the result."
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
    scope: Scope,
) -> bool:
    """Print and return whether the PR's baseline rises above the base's."""
    baseline_rel = baseline_path.relative_to(repo_root).as_posix()
    try:
        base_doc = read_base_baseline(base_sha, baseline_rel, git)
        if base_doc is None:
            print(f"No {baseline_rel} at base {base_sha}: this PR introduces it.")
            return False
        renames = renamed_paths(base_sha, git, scope.rename_root)
        promoted = promoted_rules(
            read_base_pyright_settings(base_sha, git),
            pyright_settings(repo_root / "pyproject.toml"),
            scope.severities,
        )
    except BaseRevisionError as exc:
        print(f"::error::{exc}")
        return True
    all_increases = raised_since_base(base_doc.files, head.files, renames)
    increases = raised_since_base(base_doc.files, head.files, renames, promoted)
    from_promotion = [line for line in all_increases if line not in increases]
    if from_promotion:
        print(
            f"::warning::this PR promotes {', '.join(sorted(promoted))} into the "
            f"tracked severities; {len(from_promotion)} baseline count(s) rose with "
            "it. Review them:"
        )
        for line in from_promotion:
            print(f"  ! {line}")
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
