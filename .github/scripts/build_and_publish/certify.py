#!/usr/bin/env python3
"""App certification steps of build-and-publish-app.yaml (job ``certify``).

Subcommands (each one is a former inline ``run:`` block):

``check-migration``
    v3 shape, warn-only. Runs ``tools.migrate_v3.check_migration`` from the SDK
    checkout (cwd ``sdk``) against ``../app``.

``seed-pkl-ca``
    Copy the runner's system CA bundle to ``~/.pkl-ca-bundle.pem``, the path
    app ``poe generate`` tasks pass to ``pkl --ca-certificates``.

``contract-drift``
    Enforced. In the app checkout, ``poe generate`` must leave ``generated/``
    unchanged.

``verdict``
    Render the certification summary from the step outcomes and fail when an
    enforced layer (contract drift, unit tests) failed or could not run because
    the app's dependencies did not install. An app with no ``tests/unit``
    (``UT`` = ``skipped``) still passes. Reads ``CM``, ``CD``, ``UT``, ``COV``,
    ``SDK_INSTALL``, ``APP_INSTALL``.

See docs/standards/app-certification.md.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import _bap

SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"
DOCS_URL = (
    "https://github.com/atlanhq/application-sdk/blob/main/docs/standards/"
    "app-certification.md"
)
_ICONS = {"success": "✅", "failure": "❌", "skipped": "➖", "install_failed": "❌"}


def check_migration() -> int:
    proc = _bap.run(
        [
            "uv",
            "run",
            "python",
            "-m",
            "tools.migrate_v3.check_migration",
            "--no-color",
            "../app",
        ]
    )
    if proc.returncode == 0:
        print("check_migration: no FAILs")
        return 0
    print("::warning::check_migration reported FAILs — see output above")
    return 1


def seed_pkl_ca(system_ca: str = SYSTEM_CA) -> int:
    if not Path(system_ca).is_file():
        print(
            f"::warning::System CA bundle {system_ca} not found; skipping pkl CA seed."
        )
        return 0
    home = os.environ["HOME"]
    shutil.copyfile(system_ca, f"{home}/.pkl-ca-bundle.pem")
    print(f"Seeded {home}/.pkl-ca-bundle.pem from {system_ca}")
    return 0


def contract_drift() -> int:
    if not Path("contract/app.pkl").is_file():
        print("No contract/app.pkl — skipping contract-drift check.")
        return 0
    if _bap.run(["uv", "run", "poe", "--help"], quiet=True).returncode != 0:
        print("::warning::'poe' task runner not available — cannot run drift check.")
        return 0
    if _bap.run(["uv", "run", "poe", "generate"]).returncode != 0:
        print("::error::'poe generate' failed")
        return 1
    if _bap.run(["git", "diff", "--exit-code", "--", "generated/"]).returncode != 0:
        print("::error::generated/ is stale — run 'poe generate' locally and commit.")
        return 1
    return 0


def _render(label: str, outcome: str) -> str:
    return f"- {_ICONS.get(outcome, '❓')} **{label}** — {outcome}"


def verdict() -> int:
    cm, cd, ut, cov = (_bap.env(k) for k in ("CM", "CD", "UT", "COV"))
    sdk_install = _bap.env("SDK_INSTALL")
    app_install = _bap.env("APP_INSTALL")

    cm_label = "v3 shape (check_migration) — warn-only"
    if sdk_install != "success":
        cm_label = f"{cm_label} (sdk install failed)"
    app_label = " (app install failed)" if app_install != "success" else ""

    _bap.summary(
        "### App certification",
        "",
        _render(cm_label, cm),
        _render(f"Contract drift (poe generate) — enforced{app_label}", cd),
        _render(f"Unit tests — enforced{app_label}", ut),
        _render("Coverage (>=85%) — warn-only", cov),
        "",
        "**Unit tests and contract drift block publish on ❌.** Coverage threshold "
        "and v3 shape are warn-only during rollout. See "
        f"[docs/standards/app-certification.md]({DOCS_URL}).",
    )
    if sdk_install != "success":
        print(
            "::warning title=Certification incomplete::SDK install failed, so "
            "check_migration did not run."
        )
    if cm == "failure":
        print(
            "::warning title=Certification warning (warn-only)::v3 shape failed; "
            "publish proceeded (warn-only)."
        )
    if cov == "failure":
        print(
            "::warning title=Certification warning (warn-only)::Coverage below 85%; "
            "publish proceeded (warn-only)."
        )
    if cd == "failure":
        _bap.summary(
            "",
            "### ❌ Publish blocked — contract drift",
            "",
            "`poe generate` either failed or produced changes in `app/generated/`. "
            "Run it locally and commit the diff.",
        )
        print(
            "::error title=Publish blocked::Contract drift detected. Build and publish blocked."
        )
    if ut == "failure":
        _bap.summary(
            "",
            "### ❌ Publish blocked — unit tests failed",
            "",
            "Fix the failing tests and re-run.",
        )
        print(
            "::error title=Publish blocked::Unit tests failed. Build and publish blocked."
        )
    # Fail closed: an enforced layer that could not run is not a pass. Two
    # sources, because sharded unit tests install on their own runners — the
    # app can install here (APP_INSTALL) yet not on a shard (UT).
    install_failed = app_install != "success" or ut == "install_failed"
    if install_failed:
        _bap.summary(
            "",
            "### ❌ Publish blocked — app dependencies did not install",
            "",
            "`uv sync --all-extras --frozen` failed, so contract drift and unit "
            "tests could not run. Native build dependencies go in the "
            "`apt_packages` input. See the install step's log.",
        )
        print(
            "::error title=Publish blocked::App dependencies did not install; "
            "contract drift and unit tests did not run. Build and publish blocked."
        )
    return 1 if install_failed or "failure" in (cd, ut) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("check-migration", "seed-pkl-ca", "contract-drift", "verdict"),
    )
    args = parser.parse_args(argv)
    return {
        "check-migration": check_migration,
        "seed-pkl-ca": seed_pkl_ca,
        "contract-drift": contract_drift,
        "verdict": verdict,
    }[args.command]()


if __name__ == "__main__":
    _bap.line_buffered_stdout()
    sys.exit(main())
