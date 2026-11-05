#!/usr/bin/env python3
"""Test the built conformance wheel the way a fleet repo installs it (FND-4176).

Conformance 0.51.0 passed every check this repo ran and still could not run
``gen-contract-ledger`` once installed: the checks only ever imported the
source tree, in one order. This gate installs the wheel ``uv build`` produced
into a clean venv outside the checkout and runs, against that install:

1. ``import_smoke.py`` — every module imported first in its own interpreter,
   every console script from the dist's metadata and every subcommand with
   ``--help``. ``--outside`` fails the run if ``conformance`` resolves from the
   checkout rather than the venv.
2. ``SLICE`` of the package's own tests, copied out of the checkout first. Run
   in place, pytest's rootdir insertion would put ``packages/conformance`` on
   ``sys.path`` ahead of the venv and the tests would import the source tree —
   hiding a template, schema or ``.pkl`` the wheel failed to ship.

Third-party versions come from ``packages/conformance/uv.lock`` via
``uv export --frozen``, so the gate resolves nothing fresh. That lock
path-sources the SDK and api packages from this checkout (FND-4256), which
``uv export`` writes as ``-e ../../`` lines: relative to a requirements file in
the work dir they point nowhere, and an editable install would put the checkout
back on ``sys.path``. So they are left out of the export (``LOCAL_PACKAGES``)
and installed from their source directories as ordinary, non-editable builds,
with ``--no-deps`` and then ``uv pip check``: their dependencies come from the
lock or the gate fails, never from a fresh resolve. Before any of that,
``lock_drift`` compares what the SDK and api declare (core, plus the ``[sql]``
extra the ``test`` extra selects) with what the lock recorded for them, so a
lock that predates a dependency they gained fails the gate rather than
exporting the old closure. Not ``uv lock --check``: that also fails on every
SDK or conformance version bump, which neither release flow relocks this lock
for, so it would fail every release.

Called by conformance-release.yml (after ``uv build``) and
conformance-publish.yml (between ``uv build`` and ``uv publish``).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Sequence, TypedDict, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "packages" / "conformance"
SMOKE = Path(__file__).resolve().parent / "import_smoke.py"

#: Distributions the conformance lock path-sources from this checkout, and the
#: directory each is built from. Kept out of the export, installed from here.
LOCAL_PACKAGES = {
    "atlan-application-sdk": REPO_ROOT,
    "atlan-application-sdk-api": REPO_ROOT / "packages" / "api",
}

#: The extras of each LOCAL_PACKAGES entry the conformance `test` extra pulls
#: in. Pinned to that extra's requirement by a test.
LOCAL_EXTRAS: dict[str, tuple[str, ...]] = {
    "atlan-application-sdk": ("sql",),
    "atlan-application-sdk-api": (),
}

_EXTRA_MARKER = re.compile(r"""extra\s*==\s*['"]([^'"]+)['"]""")
_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*([^;]*)")

#: (name, extras, specifiers): a requirement with spelling and order normalised.
Req = tuple[str, tuple[str, ...], tuple[str, ...]]

#: Tests that run against an install with no checkout to lean on. Chosen for
#: what the wheel must carry: the rule catalog and generated rule docs,
#: the SARIF schema and disposition engine, the deprecations manifest, the
#: bootstrap templates, and the checks the 0.51.0 cycle ran through. Tests that
#: read repo-root files (the SDK tree, .github/) cannot pass outside the
#: checkout and stay in the PR job.
SLICE = (
    "test_catalog.py",
    "test_disposition.py",
    "test_generate_rule_docs.py",
    "test_deprecations_manifest.py",
    "test_bootstrap_drift.py",
    "test_deprecation.py",
    "test_error_handling.py",
    "test_contract_compat.py",
    "test_artifact_schema_writer.py",
    "test_prescriptions.py",
)


def _run(argv: Sequence[str], cwd: Path) -> None:
    print(f"+ {' '.join(argv)}", flush=True)
    subprocess.run(argv, cwd=cwd, check=True)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _req(name: str, extras: Sequence[str], specifier: str) -> Req:
    specs = tuple(sorted(p.replace(" ", "") for p in specifier.split(",") if p.strip()))
    return _canonical(name), tuple(sorted(_canonical(e) for e in extras)), specs


def declared_requirements(pyproject: Path, extras: Sequence[str]) -> set[Req]:
    """Core and *extras* requirements of a project, from its pyproject.toml."""
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    lines = list(project.get("dependencies", []))
    for extra in extras:
        lines += project.get("optional-dependencies", {}).get(extra, [])
    out: set[Req] = set()
    for line in lines:
        match = _REQ.match(line)
        if match is None:
            raise SystemExit(f"{pyproject}: cannot parse requirement {line!r}")
        name, extra_list, spec = match.groups()
        out.add(_req(name, (extra_list or "").split(",") if extra_list else (), spec))
    return out


class _LockedRequirement(TypedDict, total=False):
    name: str
    extras: list[str]
    marker: str
    specifier: str


# Functional form: the key is spelled with a hyphen in uv.lock.
_LockedMetadata = TypedDict(
    "_LockedMetadata", {"requires-dist": list[_LockedRequirement]}, total=False
)


class _LockedPackage(TypedDict, total=False):
    name: str
    metadata: _LockedMetadata


class Lock(TypedDict, total=False):
    """The parts of a uv.lock lock_drift reads."""

    package: list[_LockedPackage]


def read_lock(lock_path: Path) -> Lock:
    return cast(Lock, tomllib.loads(lock_path.read_text(encoding="utf-8")))


def locked_requirements(
    lock: Lock, name: str, extras: Sequence[str]
) -> set[Req] | None:
    """The same requirements as the lock recorded them, or None if absent."""
    for package in lock.get("package", []):
        if package.get("name") != name:
            continue
        out: set[Req] = set()
        for entry in package.get("metadata", {}).get("requires-dist", []):
            marker = _EXTRA_MARKER.search(entry.get("marker", ""))
            if marker is None or marker.group(1) in extras:
                out.add(
                    _req(
                        entry.get("name", ""),
                        entry.get("extras", ()),
                        entry.get("specifier", ""),
                    )
                )
        return out
    return None


def lock_drift(lock_path: Path = PACKAGE_DIR / "uv.lock") -> list[str]:
    """Requirements the path-sourced packages declare that the lock has not seen.

    Requirements on the other LOCAL_PACKAGES are skipped: they pin the tree's
    own version, which moves on every release without changing a dependency.
    """
    lock = read_lock(lock_path)
    local = {_canonical(n) for n in LOCAL_PACKAGES}
    problems: list[str] = []
    for name, source in LOCAL_PACKAGES.items():
        extras = LOCAL_EXTRAS[name]
        locked = locked_requirements(lock, name, extras)
        if locked is None:
            problems.append(f"{lock_path} has no {name} entry")
            continue
        declared = declared_requirements(source / "pyproject.toml", extras)
        for req in sorted(r for r in declared - locked if r[0] not in local):
            problems.append(f"{name} declares {req} but {lock_path} does not")
        for req in sorted(r for r in locked - declared if r[0] not in local):
            problems.append(
                f"{lock_path} records {req} for {name}, which it no longer declares"
            )
    return problems


def find_wheel(wheel_dir: Path) -> Path:
    wheels = sorted(wheel_dir.glob("atlan_application_sdk_conformance-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(
            f"expected exactly one conformance wheel in {wheel_dir}, found {len(wheels)}"
        )
    return wheels[0]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--wheel-dir", type=Path, default=PACKAGE_DIR / "dist")
    args = parser.parse_args(argv)
    wheel = find_wheel(args.wheel_dir).resolve()

    with tempfile.TemporaryDirectory(prefix="conformance-wheel-gate-") as tmp:
        work = Path(tmp).resolve()
        if work.is_relative_to(REPO_ROOT):
            raise SystemExit(f"work dir {work} is inside the checkout")
        venv = work / "venv"
        python = venv / "bin" / "python"
        requirements = work / "requirements.txt"

        # The lock must describe the path-sourced SDK and api as they are now,
        # including the `[sql]` extra conformance's `test` extra selects.
        # `--frozen` exports whatever the lock holds, and `uv pip check` below
        # sees only installed metadata (no extras), so neither notices a
        # dependency the SDK gained after the lock was written.
        drift = lock_drift()
        if drift:
            for problem in drift:
                print(f"::error::{problem}", flush=True)
            print("Relock it: (cd packages/conformance && uv lock)", flush=True)
            return 1
        _run(
            [
                "uv", "export", "--frozen", "--no-hashes", "--extra", "test",
                "--no-emit-project", "--no-dev", "--output-file", str(requirements),
                *(f"--no-emit-package={name}" for name in LOCAL_PACKAGES),
            ],
            cwd=PACKAGE_DIR,
        )  # fmt: skip
        _run(["uv", "venv", "--python", sys.executable, str(venv)], cwd=work)
        _run(
            ["uv", "pip", "install", "--python", str(python), "-r", str(requirements), str(wheel)],
            cwd=work,
        )  # fmt: skip
        # --no-deps: built from source, these would otherwise resolve whatever
        # their pyproject declares from the index, so a dependency the SDK or api
        # gained before the conformance lock was refreshed would be installed at
        # a fresh, unlocked version and the gate would pass. Their dependencies
        # are already in the export, which lock_drift above proved current
        # (extras included); `uv pip check` then confirms the installed set is
        # consistent.
        _run(
            [
                "uv", "pip", "install", "--python", str(python), "--no-deps",
                *(str(path) for path in LOCAL_PACKAGES.values()),
            ],
            cwd=work,
        )  # fmt: skip
        _run(["uv", "pip", "check", "--python", str(python)], cwd=work)

        smoke = subprocess.run(
            [
                sys.executable, str(SMOKE),
                "--python", str(python),
                "--package", "conformance",
                "--dist", "atlan-application-sdk-conformance",
                "--commands-from", "conformance.cli:_COMMANDS",
                "--outside", str(REPO_ROOT),
            ],
            cwd=work,
        )  # fmt: skip
        if smoke.returncode != 0:
            return smoke.returncode

        tests = work / "tests"
        shutil.copytree(PACKAGE_DIR / "tests", tests)
        result = subprocess.run(
            [
                str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "-o", "asyncio_mode=auto", *(f"tests/{name}" for name in SLICE),
            ],
            cwd=work,
        )  # fmt: skip
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())
