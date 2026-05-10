"""
CI driver: validate atlan.yaml deploy config against platform guardrails.

Reads DEPLOY_CONFIG and SDK_VERSION from environment (populated by the
prepare job in build-and-publish-app.yaml from parse_atlan_yaml.py),
runs SDK flag injection (mirrors the version-gated defaults the chart
applies in cluster), then runs guardrail validation.

On failure, emits one ::error file=atlan.yaml:: GitHub Actions annotation
per violation so violations show inline on the PR diff in Files Changed.
Also prints a plain log line per violation so the step output is readable
in the CI log without the GH UI. Exits non-zero on any violation.

Lives outside the YAML so it can be unit-tested. Keep this file dependency-
light — only PyYAML and `packaging` are available in the entrypoint runner.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow imports of sibling modules without packaging.
sys.path.insert(0, str(Path(__file__).parent))

from config_validator import ConfigValidationError, validate_config
from sdk_version_flags import inject_sdk_version_flags


def main() -> int:
    cfg = os.environ.get("DEPLOY_CONFIG", "") or ""
    sdk = (os.environ.get("SDK_VERSION") or "").strip() or None

    if not cfg.strip():
        print("No deploy config in atlan.yaml — skipping validation")
        return 0

    # SDK flag injection runs before validation so version-gated defaults
    # (e.g. temporalWorkerDeployment at SDK >= 2.7.4) participate. Without
    # this, twc_required_for_split would fire falsely for SDK >= 2.7.4 apps
    # that rely on chart defaults.
    enriched = inject_sdk_version_flags(cfg, sdk)

    try:
        validate_config(enriched)
    except ConfigValidationError as e:
        print(f"\natlan.yaml validation failed with {len(e.violations)} violation(s):\n")
        for v in e.violations:
            # GH annotation: red error inline on atlan.yaml in PR Files view.
            print(
                f"::error file=atlan.yaml::[{v.rule}] {v.field}={v.actual!r} "
                f"(expected: {v.expected}). {v.fix}"
            )
            # Plain log line so step output is readable without GH UI.
            print(f"  - [{v.rule}] {v.field}: {v.fix}")
        return 1

    print("atlan.yaml validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
