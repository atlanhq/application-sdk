"""Report the resolved e2e source, failing fast when no source is available.

Why this exists
---------------
The dataforge credential fetch in ``tests-reusable.yaml`` runs under
``continue-on-error`` (a connector repo's own code must not be able to fail
the SDK's e2e leg on a dataforge outage) — but that also means a broken fetch
(VPN down, workload binding missing, resource gone) would otherwise let the
leg limp to the extract node and die minutes later on a cryptic
empty-credential auth failure. This step is the compensating control: it runs
with no ``continue-on-error`` and fails one minute in, with the actual fix in
the message.

Source-selection precedence (highest first):

1. **dataforge** — ``E2E_SOURCE_DATASOURCE`` is present. The fetch exports
   this non-secret breadcrumb on success, so its presence is the
   source-selection signal.
2. **repo credentials** — ``E2E_SOURCE_ENV_JSON`` is present. The connector
   repo registered its own source env map as a secret.
3. **hermetic fallback** — the connector declared
   ``dataforge-hermetic-fallback: true`` (spin-up containers, no real source).
4. **none** — error and exit 1.

The decision lives here, not inline in the workflow's ``run:`` block, because
branching in YAML cannot be regression-tested (docs/standards/ci.md). Every
message is a fixed string — credential values are never echoed, and none of
these variables carries one anyway (the env maps' presence is tested, their
contents never printed).
"""

from __future__ import annotations

import argparse
import os
import sys


def decide_source(
    e2e_source_datasource: str,
    e2e_source_env_json: str,
    hermetic_fallback: bool,
) -> tuple[int, str]:
    """Return ``(exit_code, message)`` for the resolved source selection.

    Pure function of the three inputs so every branch is pytest-covered.
    """
    if e2e_source_datasource:
        return 0, f"e2e source: dataforge ({e2e_source_datasource})"
    if e2e_source_env_json:
        return 0, (
            "e2e source: repo credentials (dataforge fetch unavailable — "
            "see steps above)"
        )
    if hermetic_fallback:
        return 0, (
            "e2e source: hermetic fallback (dataforge fetch unavailable — "
            "see steps above; no repo creds configured)"
        )
    return 1, (
        "no e2e source available — the dataforge fetch failed (see the steps "
        "above), no E2E_SOURCE_ENV_JSON repo credentials are configured, and "
        "this connector declares no hermetic source "
        "(dataforge-hermetic-fallback: false). Fix the dataforge chain (VPN, "
        "workload binding, resource) or configure E2E_SOURCE_ENV_JSON."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hermetic-fallback",
        default=os.environ.get("DF_HERMETIC_FALLBACK", ""),
        help="'true' when the connector declared dataforge-hermetic-fallback.",
    )
    args = parser.parse_args(argv)

    code, message = decide_source(
        os.environ.get("E2E_SOURCE_DATASOURCE", "").strip(),
        os.environ.get("E2E_SOURCE_ENV_JSON", "").strip(),
        args.hermetic_fallback.strip().lower() == "true",
    )
    command = "error" if code else "notice"
    print(f"::{command}::{message}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
