"""
CI driver: validate atlan.yaml deploy config against platform guardrails.

Reads DEPLOY_CONFIG and SDK_VERSION env (set by parse_atlan_yaml.py),
injects SDK version-gated defaults, runs validation, emits one
::error file=atlan.yaml:: annotation per violation. Exits non-zero on failure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

sys.path.insert(0, str(Path(__file__).parent))

from config_validator import ConfigValidationError, validate_config
from sdk_version_flags import inject_sdk_version_flags


def main() -> int:
    cfg = os.environ.get("DEPLOY_CONFIG", "") or ""
    sdk = (os.environ.get("SDK_VERSION") or "").strip() or None

    if not cfg.strip():
        print("No deploy config in atlan.yaml — skipping validation")
        return 0

    # Missing SDK_VERSION → skip validation. Without the SDK version we can't
    # apply version-gated injection (vpa/keda/TWC defaults) accurately, so
    # running validation against the un-enriched config would produce results
    # that diverge from what the chart actually renders. Better to no-op
    # than emit false-positive errors. Apps without application-sdk pinned
    # (e.g. early scaffolds) hit this path.
    if sdk is None:
        print(
            "No application-sdk version detected in atlan.yaml — skipping "
            "validation (version-gated injection cannot run without it)."
        )
        return 0

    # Bad SDK_VERSION must fail loudly: silent injection skip would validate
    # against un-enriched config, diverging from what the chart applies.
    try:
        Version(sdk)
    except InvalidVersion:
        print(
            f"::error file=atlan.yaml::SDK_VERSION '{sdk}' is not a valid "
            "PEP 440 version. SDK flag injection cannot run; validation "
            "would produce inconsistent results vs chart defaults."
        )
        print(f"  - SDK_VERSION: {sdk!r} cannot be parsed")
        return 1

    enriched = inject_sdk_version_flags(cfg, sdk)

    try:
        validate_config(enriched, sdk_version=sdk)
    except ConfigValidationError as e:
        print(
            f"\natlan.yaml validation failed with {len(e.violations)} violation(s):\n"
        )
        for v in e.violations:
            print(
                f"::error file=atlan.yaml::[{v.rule}] {v.field}={v.actual!r} "
                f"(expected: {v.expected}). {v.fix}"
            )
            print(f"  - [{v.rule}] {v.field}: {v.fix}")
        return 1

    print("atlan.yaml validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
