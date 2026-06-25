#!/usr/bin/env python3
"""
One-shot migration to add F-cross.2 compliance fields to every existing
allowlist entry. Idempotent — re-running only fills in fields that are
still missing.

Defaults are chosen to be invisible to downstream consumers until a human
fills in the real value: `null` is filtered by Pulse Tier-3 plumbing, and
`re_evaluation_trigger="expiry"` matches today's behavior (re-evaluate
when the entry expires).

Run from the repo root:

    python3 .github/scripts/migrate_allowlist_compliance_fields.py
"""

import json
import sys
from pathlib import Path

ALLOWLIST_PATH = Path(".security/base-allowlist.json")
DEFAULTS = {
    "fix_available_date": None,
    "compensating_controls": None,
    "re_evaluation_trigger": "expiry",
    "exposure_assessment": None,
    # NOTE: `suppressed` and `suppressed_reason` deliberately omitted from
    # defaults. Absence means "not yet reviewed"; setting them requires a
    # human to confirm the FP. Pulse Tier-3 plumbing treats absence and
    # False distinctly, so a default-False here would falsely assert review.
}


def main() -> int:
    data = json.loads(ALLOWLIST_PATH.read_text())
    touched = 0
    for vuln_id, entry in data.items():
        if vuln_id.startswith("_") or not isinstance(entry, dict):
            continue
        for field, default in DEFAULTS.items():
            if field not in entry:
                entry[field] = default
                touched += 1

    ALLOWLIST_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    entries = sum(1 for k in data if not k.startswith("_"))
    print(f"Migration done: {entries} entries scanned, {touched} fields added.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
