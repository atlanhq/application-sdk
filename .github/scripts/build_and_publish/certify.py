#!/usr/bin/env python3
"""App certification steps of build-and-publish-app.yaml (job ``certify``).

Subcommands (each one is a former inline ``run:`` block):

``check-migration``
    v3 shape, warn-only. Runs ``tools.migrate_v3.check_migration`` from the SDK
    checkout (cwd ``sdk``) against ``../app``.

``contract-drift``
    Enforced. In the app checkout, regenerate the contract with the SDK's own
    regeneration (``renovate_pkl_sync.regenerate``: ``pkl eval``, then
    ``contract/post-generate.sh`` if present, then ruff on generated Python),
    the same function the generated-freshness check and Renovate's toolkit
    bump use. It must leave the whole working tree clean: no modified tracked
    file and no new untracked file, anywhere (``app/generated/``, root
    ``atlan.yaml``, …). The app's own ``poe generate`` task is never run: it
    is app-defined shell, and a second generator is what let apps drift.
    A contract that does not regenerate, or a regeneration that cannot run,
    fails: the check must not pass when it could not check.

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
import sys
from collections.abc import Callable
from pathlib import Path

import _bap

# .github/scripts, which holds renovate_pkl_sync.py (stdlib only).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import renovate_pkl_sync  # noqa: E402

DOCS_URL = (
    "https://github.com/atlanhq/application-sdk/blob/main/docs/standards/"
    "app-certification.md"
)
_ICONS = {"success": "✅", "failure": "❌", "skipped": "➖", "install_failed": "❌"}
_MAX_LISTED_PATHS = 50


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


def _sdk_regeneration() -> tuple[Callable[[str], list[Path]], Callable[[str], bool]]:
    """``eval_roots`` and ``regenerate`` from ``renovate_pkl_sync``: the one
    seam tests replace."""
    return renovate_pkl_sync.eval_roots, renovate_pkl_sync.regenerate


def contract_drift(contract_dir: str = "contract") -> int:
    eval_roots, regenerate = _sdk_regeneration()
    if not eval_roots(contract_dir):
        print(f"No pkl contract in {contract_dir}/ — skipping contract-drift check.")
        return 0
    try:
        regenerated = regenerate(contract_dir)
    except OSError as exc:
        print(
            f"::error::Could not run the contract regeneration ({exc}), so "
            "contract drift could not be checked."
        )
        return 1
    if not regenerated:
        print(
            "::error::The committed contract does not regenerate (see the pkl "
            "eval error above), so contract drift could not be checked."
        )
        return 1
    status = _bap.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], capture=True
    )
    if status.returncode != 0:
        print("::error::'git status' failed, so contract drift could not be checked")
        return 1
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    if not changed:
        return 0
    # Paths only, never file contents: generated files can carry app config.
    print(
        f"::error::Regenerating the contract left {len(changed)} changed or "
        "untracked file(s) in the working tree. Run 'pkl eval --project-dir "
        "contract -m . contract/app.pkl' (and contract/post-generate.sh if the "
        "app has one), commit the result, and move any other post-processing "
        "into contract/post-generate.sh. Gitignore output that is not meant to "
        "be committed."
    )
    for line in changed[:_MAX_LISTED_PATHS]:
        print(f"  {line}")
    if len(changed) > _MAX_LISTED_PATHS:
        print(f"  … and {len(changed) - _MAX_LISTED_PATHS} more")
    return 1


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
        _render("Contract drift (SDK regeneration) — enforced", cd),
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
            "Regenerating the contract failed or changed files in the working "
            "tree. The failing step's log lists the paths and how to regenerate.",
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
            "`uv sync --all-extras --frozen` failed, so unit tests could not "
            "run. Native build dependencies go in the "
            "`apt_packages` input. See the install step's log.",
        )
        print(
            "::error title=Publish blocked::App dependencies did not install; "
            "unit tests did not run. Build and publish blocked."
        )
    return 1 if install_failed or "failure" in (cd, ut) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("check-migration", "contract-drift", "verdict"),
    )
    args = parser.parse_args(argv)
    return {
        "check-migration": check_migration,
        "contract-drift": contract_drift,
        "verdict": verdict,
    }[args.command]()


if __name__ == "__main__":
    _bap.line_buffered_stdout()
    sys.exit(main())
