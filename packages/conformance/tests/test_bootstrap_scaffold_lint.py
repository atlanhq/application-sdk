"""Lint guards for the scaffolds bootstrap force-writes into consumer repos.

FND-445: ``bootstrap`` vendors ``MANAGED_ACTION_FILES`` (currently
``.github/scripts/build_conformance_args.py`` and
``.github/actions/run-conformance-detect/action.yaml``) plus every
``MANAGED_WORKFLOWS`` shim into each consumer repo, and overwrites them
on every run — "re-running eradicates drift". A consumer whose linters
reject one of those files therefore cannot fix it: the next bootstrap
reverts the fix.

That happened. One connector repo selects pydocstyle ``D`` and sets
``line-length = 100``; the shipped ``build_conformance_args.py`` had no
function docstrings, a backslash in a non-raw module docstring, and a
call split at 88 columns that ``ruff format`` re-joined at 100. Result:
five consecutive red ``checks.yml`` runs on ``main``, a permanently red
pre-commit, and an otherwise-clean remediation PR held in draft — all
from a file the repo does not own.

So the rule these tests hold: **anything bootstrap force-writes has to
satisfy the strictest linter config in the fleet, not just this repo's.**
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest
from conformance.bootstrap.render import MANAGED_ACTION_FILES, MANAGED_WORKFLOWS, render

# The strictest ruff lint selection seen in the fleet, plus "I" (import
# order), because fleet repos also run isort. A repo tightening its config
# further is exactly how FND-445 surfaced, so widen this list when it does.
_STRICT_SELECT = "E,F,W,I,D,G,T201,LOG,BLE,S110,S112,TRY400,TRY401"

# Every line length a caller plausibly configures. Both check (E501) and
# format (re-wrapping) run at each: a scaffold formatted for one width is
# not automatically a format no-op at another, which is half of what made
# the affected repo red.
_LINE_LENGTHS = (88, 100, 120)

# The tightest wrap in _LINE_LENGTHS. Keeping every physical line inside
# it, and closing every exploded call/signature with a magic trailing
# comma, is what makes `ruff format` a no-op at all of them.
_TIGHTEST_WRAP = min(_LINE_LENGTHS)

_PY_SCAFFOLDS = tuple(
    (dest, template) for dest, template in MANAGED_ACTION_FILES if dest.endswith(".py")
)

# Every always-overwrite artifact, as (repo-relative dest, template name).
# renovate.json and .gitignore are excluded on purpose: those are
# write-if-absent, so a consumer can edit them and keep the edit.
_FORCE_WRITTEN = (
    *MANAGED_ACTION_FILES,
    *((f".github/workflows/{name}", name) for name in MANAGED_WORKFLOWS),
    (".claude/skills/remediate/SKILL.md", "remediate.md"),
)


def _ruff_cmd() -> list[str]:
    """Return the argv prefix that runs ruff in this environment."""
    exe = shutil.which("ruff")
    return [exe] if exe else [sys.executable, "-m", "ruff"]


def _write_scaffold(
    tmp_path: pathlib.Path, dest_rel: str, template: str
) -> pathlib.Path:
    """Render *template* to a file named as the consumer repo sees it."""
    path = tmp_path / pathlib.Path(dest_rel).name
    path.write_text(render(template), encoding="utf-8")
    return path


def test_ruff_is_installed() -> None:
    """Fail loudly rather than let the guards below vanish into a skip."""
    proc = subprocess.run([*_ruff_cmd(), "--version"], capture_output=True, text=True)
    assert proc.returncode == 0, (
        "ruff is not runnable here, so the scaffold lint guards cannot run. It "
        "is a pinned dev-group dependency of this package — `uv sync "
        "--all-extras --all-groups`, which is what CI does."
    )


def test_there_is_a_python_scaffold_to_lint() -> None:
    """Guard the guard: an empty parametrize list would pass silently."""
    assert _PY_SCAFFOLDS, (
        "MANAGED_ACTION_FILES has no .py entry — if the vendored script was "
        "renamed or retired, update _PY_SCAFFOLDS; if it moved to a new "
        "extension, add the equivalent lint guard for it."
    )


@pytest.mark.parametrize("line_length", _LINE_LENGTHS)
@pytest.mark.parametrize("dest_rel,template", _PY_SCAFFOLDS)
def test_python_scaffold_passes_strict_ruff_check(
    dest_rel: str,
    template: str,
    line_length: int,
    tmp_path: pathlib.Path,
) -> None:
    path = _write_scaffold(tmp_path, dest_rel, template)
    proc = subprocess.run(
        [
            *_ruff_cmd(),
            "check",
            # --isolated: judge the file as a foreign repo's linter would,
            # not through this repo's pyproject.toml (which selects neither
            # D nor a non-default line length — why this stayed latent).
            "--isolated",
            "--line-length",
            str(line_length),
            "--select",
            _STRICT_SELECT,
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"{dest_rel} fails `ruff check --select {_STRICT_SELECT}` at "
        f"line-length {line_length}. Consumer repos cannot fix this — "
        f"bootstrap force-writes the file — so fix the template:\n"
        f"{proc.stdout}{proc.stderr}"
    )


@pytest.mark.parametrize("line_length", _LINE_LENGTHS)
@pytest.mark.parametrize("dest_rel,template", _PY_SCAFFOLDS)
def test_python_scaffold_is_a_ruff_format_no_op(
    dest_rel: str,
    template: str,
    line_length: int,
    tmp_path: pathlib.Path,
) -> None:
    path = _write_scaffold(tmp_path, dest_rel, template)
    proc = subprocess.run(
        [
            *_ruff_cmd(),
            "format",
            "--isolated",
            "--line-length",
            str(line_length),
            "--check",
            "--diff",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"{dest_rel} is not `ruff format`-clean at line-length "
        f"{line_length}, so a consumer's ruff-format hook rewrites it and "
        f"reds their pre-commit on every run. Close exploded calls with a "
        f"magic trailing comma so the shape holds at every width:\n"
        f"{proc.stdout}{proc.stderr}"
    )


@pytest.mark.parametrize("dest_rel,template", _PY_SCAFFOLDS)
def test_python_scaffold_lines_fit_the_tightest_wrap(
    dest_rel: str, template: str
) -> None:
    """Keep every physical line inside the tightest configured wrap.

    E501 at the tightest width would catch this too; asserting it
    directly names the actual invariant, so the failure says "shorten
    line 42" rather than pointing at a formatter disagreement.
    """
    long_lines = [
        (num, len(line))
        for num, line in enumerate(render(template).splitlines(), start=1)
        if len(line) > _TIGHTEST_WRAP
    ]
    assert not long_lines, (
        f"{dest_rel} has lines longer than {_TIGHTEST_WRAP} columns "
        f"(line, length): {long_lines}. A caller wrapping at "
        f"{_TIGHTEST_WRAP} would both flag E501 and re-wrap the file."
    )


@pytest.mark.parametrize("dest_rel,template", _FORCE_WRITTEN)
def test_force_written_scaffold_is_whitespace_clean(
    dest_rel: str, template: str
) -> None:
    """Hold the pre-commit-hooks staples for every force-written file.

    trailing-whitespace, end-of-file-fixer and mixed-line-ending are the
    hooks fleet repos run alongside ruff; a file bootstrap keeps
    rewriting has to satisfy them too, whatever its type — YAML and
    Markdown included, not just the vendored Python.
    """
    content = render(template)
    assert "\r" not in content, f"{dest_rel} contains a CR (mixed line endings)"
    assert "\t" not in content, f"{dest_rel} contains a tab"
    assert content.endswith("\n"), f"{dest_rel} does not end with a newline"
    assert not content.endswith("\n\n"), f"{dest_rel} ends with a blank line"
    trailing = [
        num
        for num, line in enumerate(content.splitlines(), start=1)
        if line != line.rstrip()
    ]
    assert not trailing, f"{dest_rel} has trailing whitespace on lines {trailing}"
