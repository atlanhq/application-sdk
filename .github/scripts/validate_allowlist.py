#!/usr/bin/env python3
"""
Validate base-allowlist.json entries against the expiry policy.

Rules enforced:
  1. Every entry must have: package, severity, reason, expires, added_by,
     case, ticket
  2. `expires` must be a valid YYYY-MM-DD date
  3. `expires` must not already be in the past
  4. `expires` must be within the max window for the entry's severity:
       CRITICAL → _expiry_policy.CRITICAL days (default 7)
       HIGH     → _expiry_policy.HIGH days     (default 30)
     Only CRITICAL and HIGH are allowlistable — MEDIUM/LOW are tracked on
     Linear tickets only (their SLAs are not gated), so any other severity
     is rejected here.
  5. `case` must be one of 1|2|3|4 (the vuln-triage classification), `ticket`
     the Linear identifier driving the entry, and `fix_pr` (optional) a URL.

The expiry windows ARE the remediation SLA, measured from first detection:
CRITICAL 7 days, HIGH 30 days. They are read from the `_expiry_policy`
metadata key in the allowlist itself so the security team can adjust limits
without touching code:

  {
    "_expiry_policy": { "CRITICAL": 7, "HIGH": 30 },
    "CVE-2026-12345": { "case": 1, "ticket": "BLDX-1234", ... }
  }
"""

import json
import sys
from datetime import datetime, timedelta

ALLOWLIST_PATH = ".security/base-allowlist.json"
DEFAULT_POLICY = {"CRITICAL": 7, "HIGH": 30}
REQUIRED_FIELDS = [
    "package",
    "severity",
    "reason",
    "expires",
    "added_by",
    "case",
    "ticket",
]

# Valid vuln-triage classification cases (see .mothership/vuln-triage/ORCHESTRATION.md).
VALID_CASES = {"1", "2", "3", "4"}

# F-cross.2 compliance fields. Optional during the migration window; promoted
# to required once all entries are filled in.
VALID_TRIGGERS = ("fix_released", "quarterly", "expiry", "kev_listed")


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

        # 5. Compliance fields — type-check when present, not required yet.
        trig = entry.get("re_evaluation_trigger")
        if trig is not None and trig not in VALID_TRIGGERS:
            errors.append(
                f"{cve_id}: re_evaluation_trigger '{trig}' must be one of "
                f"{VALID_TRIGGERS}"
            )

        fad = entry.get("fix_available_date")
        if fad:  # None / empty string allowed during migration
            try:
                datetime.strptime(fad, "%Y-%m-%d")
            except ValueError:
                errors.append(
                    f"{cve_id}: fix_available_date '{fad}' must be YYYY-MM-DD"
                )

        sup = entry.get("suppressed")
        if sup is not None and not isinstance(sup, bool):
            errors.append(
                f"{cve_id}: suppressed must be a bool, got {type(sup).__name__}"
            )

        # 6. Triage metadata: case (1-4) and an optional fix PR URL.
        case = entry.get("case")
        if str(case) not in VALID_CASES:
            errors.append(
                f"{cve_id}: case '{case}' must be one of 1, 2, 3, 4 "
                f"(vuln-triage classification)"
            )

        fix_pr = entry.get("fix_pr")
        if fix_pr is not None and not str(fix_pr).startswith("https://"):
            errors.append(
                f"{cve_id}: fix_pr '{fix_pr}' must be an https URL to the fix PR"
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
