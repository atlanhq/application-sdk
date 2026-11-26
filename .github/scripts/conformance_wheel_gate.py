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

Third-party versions come from the root ``uv.lock`` via ``uv export
--locked --package atlan-application-sdk-conformance``, so the gate resolves
nothing fresh. ``packages/conformance`` is a member of the root uv workspace
(FND-4671), so that one lock records conformance and the SDK it tests against.
``--locked`` fails the gate if the lock no longer matches any pyproject in the
workspace, extras included, so a dependency the SDK gained after the lock was
written stops the release rather than exporting the old closure. The SDK and
api are workspace or path sources in this checkout, which ``uv export`` writes
as ``-e`` lines: an editable install would put the checkout back on
``sys.path``. So they are left out of the export (``LOCAL_PACKAGES``) and
installed from their source directories as ordinary, non-editable builds, with
``--no-deps`` and then ``uv pip check``: their dependencies come from the lock
or the gate fails, never from a fresh resolve.

Called by conformance-release.yml (after ``uv build``) and
conformance-publish.yml (between ``uv build`` and ``uv publish``).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "packages" / "conformance"
SMOKE = Path(__file__).resolve().parent / "import_smoke.py"

#: Distributions the conformance export would take from this checkout, and the
#: directory each is built from. Kept out of the export, installed from here.
LOCAL_PACKAGES = {
    "atlan-application-sdk": REPO_ROOT,
    "atlan-application-sdk-api": REPO_ROOT / "packages" / "api",
}

#: The workspace member whose closure the gate exports.
DIST = "atlan-application-sdk-conformance"

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

#: Tests inside SLICE files that compare package data against the SDK source
#: tree, so they need the monorepo checkout. ``tests._helpers.sdk_root`` fails
#: such a test under ``CI`` (FND-4948) rather than skipping it, and the gate
#: runs with ``CI`` set from a copy outside the checkout. The PR job runs them
#: from the checkout, where the comparison means something.
DESELECT = (
    "test_deprecations_manifest.py::test_committed_manifest_matches_fresh_scan",
)


def _run(argv: Sequence[str], cwd: Path) -> None:
    print(f"+ {' '.join(argv)}", flush=True)
    subprocess.run(argv, cwd=cwd, check=True)


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

        _run(
            [
                "uv", "export", "--locked", "--no-hashes", "--package", DIST,
                "--extra", "test", "--no-emit-project", "--no-dev",
                "--output-file", str(requirements),
                *(f"--no-emit-package={name}" for name in LOCAL_PACKAGES),
            ],
            cwd=REPO_ROOT,
        )  # fmt: skip
        _run(["uv", "venv", "--python", sys.executable, str(venv)], cwd=work)
        _run(
            ["uv", "pip", "install", "--python", str(python), "-r", str(requirements), str(wheel)],
            cwd=work,
        )  # fmt: skip
        # --no-deps: built from source, these would otherwise resolve whatever
        # their pyproject declares from the index, at a fresh, unlocked version.
        # Their dependencies are already in the export, which `--locked` above
        # proved current (extras included); `uv pip check` then confirms the
        # installed set is consistent.
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
                "-o", "asyncio_mode=auto",
                *(arg for node in DESELECT for arg in ("--deselect", f"tests/{node}")),
                *(f"tests/{name}" for name in SLICE),
            ],
            cwd=work,
        )  # fmt: skip
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())
