"""Append-only guard for the contract schema ledger.

Validates that no entry was deleted from ``contract_schema.lock.json`` and no
recorded ``type`` was changed between the base ref and HEAD.  Status changes
and additions are always allowed.

Exit codes
----------
0  All checks pass (no deletions, no type changes).
1  A deletion or type change was detected — block the PR.
2  Usage error (bad arguments, missing base-ref ledger, etc.).

Usage
-----
Called from CI after ``fetch-depth: 0`` so the full git history is available:

    python .github/scripts/contract_ledger_guard.py \\
        --base-ref origin/main \\
        --ledger-path packages/conformance/conformance/data/contract_schema.lock.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _load_json_from_git(ref: str, path: str) -> dict | None:
    """Return parsed JSON at *path* at git ref *ref*, or None if absent."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _load_json_from_disk(path: Path) -> dict | None:
    """Return parsed JSON from *path* on disk, or None if absent/malformed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _index_fields(payload: dict) -> dict[tuple[str, str], str]:
    """Index ledger fields as {(contract, field): type}."""
    result: dict[tuple[str, str], str] = {}
    for entry in payload.get("fields", []):
        key = (entry.get("contract", ""), entry.get("field", ""))
        result[key] = entry.get("type", "")
    return result


def check(
    base_payload: dict | None,
    head_payload: dict | None,
) -> tuple[bool, list[str]]:
    """Compare base and head ledger payloads.

    Returns (passed, error_messages).  ``passed`` is True when no deletions or
    type changes are detected.  An absent *base_payload* (no prior ledger) means
    there is nothing to guard against — passes with no errors.
    """
    errors: list[str] = []

    if base_payload is None:
        return True, errors

    if head_payload is None:
        errors.append(
            "HEAD ledger is absent but base ledger exists — "
            "the file appears to have been deleted."
        )
        return False, errors

    base_index = _index_fields(base_payload)
    head_index = _index_fields(head_payload)

    for (contract, field), base_type in base_index.items():
        if (contract, field) not in head_index:
            errors.append(
                f"DELETED: {contract}.{field} (type: {base_type!r}) — "
                "ledger entries are permanent. Mark the field 'deprecated' or "
                "'sunset' in source and regenerate instead of deleting the entry."
            )
        else:
            head_type = head_index[(contract, field)]
            if head_type != base_type:
                errors.append(
                    f"TYPE CHANGED: {contract}.{field} "
                    f"'{base_type}' → '{head_type}' — "
                    "a recorded type is frozen on first record and can never change."
                )

    return len(errors) == 0, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Append-only guard for the entrypoint-contract ledger.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Git ref for the base (default: origin/main).",
    )
    parser.add_argument(
        "--ledger-path",
        required=True,
        help="Repo-relative path to the ledger file (e.g. packages/conformance/conformance/data/contract_schema.lock.json).",
    )
    args = parser.parse_args(argv)

    ledger_path = Path(args.ledger_path)

    base_payload = _load_json_from_git(args.base_ref, args.ledger_path)
    head_payload = _load_json_from_disk(ledger_path)

    passed, errors = check(base_payload, head_payload)

    if passed:
        field_count = len(_index_fields(head_payload)) if head_payload else 0
        print(f"Contract ledger guard: OK ({field_count} entries).")
        return 0

    print("Contract ledger guard: FAILED", file=sys.stderr)
    for msg in errors:
        print(f"  {msg}", file=sys.stderr)
    print(
        "\nThe contract_schema.lock.json ledger is append-only. "
        "Field deletions and type changes are not permitted.\n"
        "To retire a field: mark it 'deprecated' or 'sunset' in the widget "
        "definition and regenerate with:\n"
        "  uv run atlan-application-sdk-conformance gen-contract-ledger",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
