"""Fail if ruff would change any Python file the toolkit generates.

Apps run ruff on ``app/generated/*.py`` in pre-commit. If the toolkit's raw
output is not already ruff-clean, what an app commits differs from what
``pkl eval`` writes, and the contract-drift gate fails even when the contract
has not changed.

For every ``examples/*/app.pkl`` this script:

1. evaluates the contract into a temporary directory outside this repo, so
   ruff sees the same layout as an app repo (``application_sdk`` is a
   third-party package there, not first-party as it is inside the SDK repo);
2. runs ``ruff check --fix`` (default rules plus ``I``) and ``ruff format``
   with ``--isolated``, i.e. ruff's default settings;
3. reports every generated ``.py`` file ruff changed, with a diff;
4. fails on any lint error ruff cannot fix (``F821`` undefined name, ...),
   which an app's pre-commit would also reject.

It also runs ruff on ``tests/fixtures/py_format/input.py.txt`` and compares
the result with ``expected.py.txt``, which ``tests/py_format_test.pkl`` holds
``PyFormat.format`` to. That fixture covers layouts the examples do not reach,
and it starts from unformatted input: ruff keeps a trailing comma it finds, so
running it on the toolkit's own output cannot tell whether the toolkit put one
where ruff would not.

ruff comes from the environment running this script (the SDK's locked dev
dependency), so the version is pinned in ``uv.lock``. Run with:

    uv run python contract-toolkit/scripts/check-ruff-stable.py [example ...]
"""

import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = TOOLKIT_ROOT / "examples"
FIXTURE_DIR = TOOLKIT_ROOT / "tests" / "fixtures" / "py_format"


def _run(cmd: list[str], cwd: Path) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"command failed ({result.returncode}): {' '.join(cmd)}")
        print(result.stdout)
        print(result.stderr)
        sys.exit(2)


def _generate(examples: list[Path], out_root: Path) -> list[Path]:
    """Evaluate each example into ``out_root/<name>``; return generated .py files."""
    for example in examples:
        _run(
            [
                "pkl",
                "eval",
                "-m",
                str(out_root / example.name),
                str(example / "app.pkl"),
            ],
            TOOLKIT_ROOT,
        )
    return sorted(p.relative_to(out_root) for p in out_root.rglob("*.py"))


def _ruff(args: list[str], files: list[Path], cwd: Path) -> None:
    ruff = [sys.executable, "-m", "ruff"]
    # Let ruff rewrite in place; main() diffs the result against the raw copy.
    _run([*ruff, *args, "--isolated", "--quiet", *map(str, files)], cwd)


def _diff(before: str, after: str, before_name: str, after_name: str) -> bool:
    """Print a unified diff; return True when the two texts differ."""
    if before == after:
        return False
    sys.stdout.writelines(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            before_name,
            after_name,
        )
    )
    return True


def _check_fixture(tmp: Path) -> bool:
    """Return True when ruff's layout of the fixture input differs from expected."""
    work = tmp / "fixture"
    work.mkdir()
    target = work / "fixture.py"
    shutil.copyfile(FIXTURE_DIR / "input.py.txt", target)
    # Import sorting only: the fixture's names are deliberately undefined.
    _ruff(["check", "--fix", "--exit-zero", "--select", "I"], [target.name], work)
    _ruff(["format"], [target.name], work)
    return _diff(
        (FIXTURE_DIR / "expected.py.txt").read_text(),
        target.read_text(),
        "tests/fixtures/py_format/expected.py.txt",
        "ruff/input.py.txt",
    )


def main(argv: list[str]) -> int:
    if argv:
        examples = [EXAMPLES_DIR / name for name in argv]
    else:
        examples = sorted(p.parent for p in EXAMPLES_DIR.glob("*/app.pkl"))

    with tempfile.TemporaryDirectory(prefix="toolkit-ruff-") as tmp:
        # resolve(): pkl rejects outputs whose real path (macOS /private/var)
        # differs from the symlinked output directory it was given.
        raw_root = Path(tmp).resolve() / "raw"
        fmt_root = Path(tmp).resolve() / "formatted"
        files = _generate(examples, raw_root)
        shutil.copytree(raw_root, fmt_root)

        _ruff(
            ["check", "--fix", "--exit-zero", "--extend-select", "I"], files, fmt_root
        )
        _ruff(["format"], files, fmt_root)

        changed = sum(
            _diff(
                (raw_root / rel).read_text(),
                (fmt_root / rel).read_text(),
                f"toolkit/{rel}",
                f"ruff/{rel}",
            )
            for rel in files
        )
        # What --fix could not fix is still an error in an app's pre-commit.
        lint = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--isolated",
                "--extend-select",
                "I",
                *map(str, files),
            ],
            cwd=fmt_root,
            capture_output=True,
            text=True,
        )
        if lint.returncode != 0:
            print(lint.stdout)
            print(lint.stderr)
        fixture_stale = _check_fixture(Path(tmp).resolve())

    print(f"\n{changed} of {len(files)} generated Python files are not ruff-stable.")
    if changed:
        print(
            "Fix the renderer in contract-toolkit/src (see src/PyFormat.pkl); do not format the output."
        )
    if lint.returncode != 0:
        print(
            "Generated Python has lint errors ruff cannot fix (above). Fix the renderer."
        )
    if fixture_stale:
        print(
            "ruff no longer lays out tests/fixtures/py_format/input.py.txt as expected.py.txt "
            "says: update expected.py.txt to ruff's output, then PyFormat.pkl until "
            "tests/py_format_test.pkl passes."
        )
    return 1 if changed or fixture_stale or lint.returncode != 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
