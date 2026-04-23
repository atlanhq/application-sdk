#!/usr/bin/env python3
"""
Validate base-allowlist.json entries against the expiry policy.

Rules enforced:
  1. Every entry must have: package, severity, reason, expires, added_by
  2. `expires` must be a valid YYYY-MM-DD date
  3. `expires` must not already be in the past
  4. `expires` must be within the max window for the entry's severity:
       CRITICAL → _expiry_policy.CRITICAL days (default 30)
       HIGH     → _expiry_policy.HIGH days     (default 60)

The expiry policy is read from the `_expiry_policy` metadata key in the
allowlist itself so the security team can adjust limits without touching code:

  {
    "_expiry_policy": { "CRITICAL": 30, "HIGH": 60 },
    "CVE-2026-12345": { ... }
  }
"""

import json
import sys
from datetime import datetime, timedelta

ALLOWLIST_PATH = ".security/base-allowlist.json"
DEFAULT_POLICY = {"CRITICAL": 30, "HIGH": 60}
REQUIRED_FIELDS = ["package", "severity", "reason", "expires", "added_by"]


def main() -> int:
    try:
        with open(ALLOWLIST_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"No {ALLOWLIST_PATH} found — nothing to validate.")
        return 0
    except json.JSONDecodeError as e:
        print(f"❌ {ALLOWLIST_PATH} is not valid JSON: {e}")
        return 1

    policy = {**DEFAULT_POLICY, **data.get("_expiry_policy", {})}
    today = datetime.utcnow().date()
    errors = []

    entries = {k: v for k, v in data.items() if not k.startswith("_")}

    for cve_id, entry in entries.items():
        if not isinstance(entry, dict):
            errors.append(f"{cve_id}: entry must be a JSON object")
            continue

        # 1. Required fields
        missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
        if missing:
            errors.append(f"{cve_id}: missing required field(s): {', '.join(missing)}")
            continue

        severity = entry["severity"].upper()
        expires_str = entry["expires"]

        # 2. Valid date format
        try:
            exp_date = datetime.strptime(expires_str, "%Y-%m-%d").date()
        except ValueError:
            errors.append(
                f"{cve_id}: invalid expires format '{expires_str}' — use YYYY-MM-DD"
            )
            continue

        # 3. Not already expired
        if exp_date < today:
            errors.append(
                f"{cve_id}: already expired ({expires_str}) — remove the entry or renew it"
            )
            continue

        # 4. Within policy window
        max_days = policy.get(severity)
        if max_days is None:
            errors.append(
                f"{cve_id}: unknown severity '{severity}' — must be CRITICAL or HIGH"
            )
            continue

        days_from_now = (exp_date - today).days
        max_date = today + timedelta(days=max_days)

        if exp_date > max_date:
            errors.append(
                f"{cve_id} ({severity}): expires {expires_str} is {days_from_now} days away "
                f"— max allowed for {severity} is {max_days} days (by {max_date})"
            )

    if errors:
        print(f"❌ Allowlist validation failed ({len(errors)} error(s)):\n")
        for e in errors:
            print(f"  • {e}")
        print(
            f"\nPolicy: CRITICAL ≤ {policy['CRITICAL']} days, HIGH ≤ {policy['HIGH']} days"
            f"\nTo change limits, update '_expiry_policy' in {ALLOWLIST_PATH}."
        )
        return 1

    print(
        f"✅ Allowlist valid: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}, "
        f"all within policy (CRITICAL ≤ {policy['CRITICAL']}d, HIGH ≤ {policy['HIGH']}d)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
