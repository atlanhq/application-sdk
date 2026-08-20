"""Fail at discovery when the repo lacks the full-DAG e2e scaffold it needs.

Why this exists (FND-656 B2)
---------------------------
``tests-reusable.yaml``'s e2e job pins the full-DAG per-repo overrides to fixed
paths under ``.github/e2e/`` — the config dir, and the secrets script that writes
``<config-dir>/secrets/credentials.json``. It has to pin them: a repo carrying
BOTH an SDR (testcontainer) setup under ``.github/sdr-e2e/`` and a full-DAG setup
under ``.github/e2e/`` would otherwise auto-resolve to the SDR one and run the
full-DAG legs against the wrong secrets and app.yaml.

The cost of pinning is that a repo *without* that scaffold turns an absent
optional directory into a hard failure — and it fails in the worst possible
place. The ``sdr-e2e`` action resolves those paths inside each e2e leg, which is
downstream of two per-arch image builds, a manifest merge, a tenant lease and a
tenant install: ~40 minutes of runner and live-tenant time before a message that
says ``config-dir '.github/e2e' not found`` — which reads as a bad input to the
reusable rather than as "this repo was never onboarded to the full-DAG tier".
Observed across the FND-402 fleet sweep on 11 connectors.

Everything checked here is knowable from a checkout in milliseconds, so this runs
in ``discover-e2e`` — the first job on the e2e path — for the same reason the
tenant-matrix precondition does (FND-203).

Scope: exactly what the e2e job pins, never more
------------------------------------------------
This must not be able to red a repo that would otherwise have passed, so it
checks the paths the caller pins and the one file the action hard-requires, and
nothing else:

* the pinned config dir exists,
* an ``app.yaml`` is resolvable (config dir first, then repo root — the action's
  own order),
* the pinned full-DAG secrets script exists.

``components-dir`` and ``compose-overlay`` are deliberately NOT checked: a
missing overlay is skipped silently by the compose chain builder and a missing
components dir falls back to the SDK defaults, so requiring them here would fail
runs that work today.

Reports every missing piece in one pass rather than stopping at the first, so an
un-onboarded repo learns the whole gap from one run instead of one file per
20-minute round trip.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

#: The full-DAG config dir ``tests-reusable.yaml`` pins via ``config-dir``.
FULL_DAG_CONFIG_DIR = ".github/e2e"

#: The SDR (testcontainer) convention. Only ever mentioned in diagnostics: a repo
#: carrying this and not :data:`FULL_DAG_CONFIG_DIR` has an SDR setup and no
#: full-DAG one, which is a materially different fix from having neither.
SDR_CONFIG_DIR = ".github/sdr-e2e"

#: The secrets script ``tests-reusable.yaml`` pins via ``secrets-script``. The
#: sdr-e2e action hard-fails on its absence (it is what writes
#: ``<config-dir>/secrets/credentials.json``).
FULL_DAG_SECRETS_SCRIPT = ".github/e2e/make-secrets-e2e-full.py"

_DOCS = "docs/standards/connector-ci-e2e.md"


@dataclass(frozen=True)
class Gap:
    """One missing piece of the scaffold, with the remedy for it."""

    path: str
    remedy: str


def find_gaps(*, root: Path = Path(".")) -> list[Gap]:
    """Return every missing piece of the full-DAG e2e scaffold under *root*.

    Empty means the repo carries what the e2e job pins. Order is the order the
    ``sdr-e2e`` action would hit them in, so the list reads as the sequence of
    failures it prevents.
    """
    gaps: list[Gap] = []

    config_dir = root / FULL_DAG_CONFIG_DIR
    if not config_dir.is_dir():
        # Name the SDR dir when it is the one that exists: "you have the SDR
        # scaffold, not the full-DAG one" is a different job from "you have
        # neither", and the two are indistinguishable from the path alone.
        if (root / SDR_CONFIG_DIR).is_dir():
            remedy = (
                f"this repo has {SDR_CONFIG_DIR}/ (the SDR / testcontainer tier) "
                f"but no {FULL_DAG_CONFIG_DIR}/ (the full-DAG tier). They are "
                "separate stacks with separate secrets and app.yaml, and the e2e "
                f"job pins {FULL_DAG_CONFIG_DIR} deliberately so a repo with both "
                "cannot silently run full-DAG legs against the SDR setup. Add the "
                f"full-DAG scaffold under {FULL_DAG_CONFIG_DIR}/, or set "
                "'enable-e2e: false' in this repo's tests.yaml until it is "
                "onboarded."
            )
        else:
            remedy = (
                f"create {FULL_DAG_CONFIG_DIR}/ holding the full-DAG app.yaml and "
                "the secrets script below, or set 'enable-e2e: false' in this "
                "repo's tests.yaml until the connector is onboarded to the "
                "full-DAG tier."
            )
        gaps.append(Gap(FULL_DAG_CONFIG_DIR, remedy))

    # The action's own resolution order: app.yaml inside the config dir wins,
    # falling back to a repo-root one. Checked independently of the dir above so
    # a repo that has the dir but no app.yaml learns both in one pass.
    if not (config_dir / "app.yaml").is_file() and not (root / "app.yaml").is_file():
        gaps.append(
            Gap(
                f"{FULL_DAG_CONFIG_DIR}/app.yaml",
                "add the configurator input the e2e job renders with envsubst "
                f"(or a repo-root app.yaml, which the action also accepts). See "
                f"{_DOCS}.",
            )
        )

    if not (root / FULL_DAG_SECRETS_SCRIPT).is_file():
        gaps.append(
            Gap(
                FULL_DAG_SECRETS_SCRIPT,
                "add the script that writes "
                f"{FULL_DAG_CONFIG_DIR}/secrets/credentials.json from the env "
                "vars E2E_SOURCE_ENV_JSON exports. The e2e job pins this exact "
                "path; the sdr-e2e action fails the leg without it.",
            )
        )

    return gaps


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Directory the existence checks run against (default: cwd).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    gaps = find_gaps(root=args.root)
    if not gaps:
        print(
            "Full-DAG e2e scaffold present "
            f"({FULL_DAG_CONFIG_DIR}/, app.yaml, {FULL_DAG_SECRETS_SCRIPT}).",
            file=sys.stderr,
        )
        return 0

    detail = " ".join(f"MISSING {gap.path} — {gap.remedy}" for gap in gaps)
    print(
        f"::error::e2e was requested but this repo is missing {len(gaps)} piece(s) "
        "of the full-DAG e2e scaffold that tests-reusable.yaml's e2e job pins, so "
        "every leg would fail inside the sdr-e2e action — after two image builds, "
        "a tenant lease and a tenant install. Failing at discovery instead, in "
        f"seconds. {detail}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
