"""Append-only guard for the contract schema ledger.

Validates that no entry was deleted from ``contract_schema.lock.json``, no
recorded ``type`` was changed, and no entry moved straight from ``active`` to
``sunset`` between the base ref and HEAD.  Additions and every other status
change are allowed.  Retirement runs ``active`` → ``deprecated`` → ``sunset``
across separate merges, so callers always see a deprecation before a field is
withdrawn.

Exit codes
----------
0  All checks pass (no deletions, no type changes, no skipped deprecation).
1  A deletion, type change or ``active`` → ``sunset`` move was detected — block
   the PR.
2  Usage error (bad arguments, missing base-ref ledger, etc.).

Usage
-----
Called from CI after ``fetch-depth: 0`` so the full git history is available:

    uvx atlan-application-sdk-conformance ledger-guard \\
        --base-ref origin/main \\
        --ledger-path contract_schema.lock.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import detect_scope
from conformance.suite.checks.deprecation._ledger_schema import regen_command


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


_STATUSES = frozenset({"active", "deprecated", "sunset"})


def _index_statuses(payload: dict) -> dict[tuple[str, str], str]:
    """Index ledger statuses as {(contract, field): status}.

    An absent or null status means active, as the ledger loader reads it.
    """
    return {
        (entry.get("contract", ""), entry.get("field", "")): entry.get("status")
        or "active"
        for entry in payload.get("fields", [])
    }


def check(
    base_payload: dict | None,
    head_payload: dict | None,
) -> tuple[bool, list[str]]:
    """Compare base and head ledger payloads.

    Returns (passed, error_messages).  ``passed`` is True when no deletions,
    type changes, ``active`` → ``sunset`` moves or unknown HEAD statuses are
    detected.  An absent *base_payload* (no prior ledger) means there is
    nothing to guard against — passes with no errors.
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
    base_status = _index_statuses(base_payload)
    head_status = _index_statuses(head_payload)

    for (contract, field), base_type in base_index.items():
        if (contract, field) not in head_index:
            errors.append(
                f"DELETED: {contract}.{field} (type: {base_type!r}) — "
                "ledger entries are permanent. Mark the field 'deprecated' in "
                "source and regenerate instead of deleting the entry; it may "
                "move to 'sunset' in a later PR."
            )
        else:
            head_type = head_index[(contract, field)]
            if head_type != base_type:
                errors.append(
                    f"TYPE CHANGED: {contract}.{field} "
                    f"'{base_type}' → '{head_type}' — "
                    "a recorded type is frozen on first record and can never change."
                )
            if (
                base_status[(contract, field)] == "active"
                and head_status[(contract, field)] == "sunset"
            ):
                errors.append(
                    f"SUNSET WITHOUT DEPRECATION: {contract}.{field} "
                    "'active' → 'sunset' — mark it 'deprecated' first and merge "
                    "that, then move it to 'sunset' in a later PR."
                )

    for (contract, field), status in head_status.items():
        if status not in _STATUSES:
            errors.append(
                f"INVALID STATUS: {contract}.{field} {status!r} — a ledger "
                "status is one of 'active', 'deprecated' or 'sunset'; "
                "regenerate instead of editing it by hand."
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
        help="Repo-relative path to the ledger file (e.g. contract_schema.lock.json).",
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
        "Field deletions and type changes are not permitted, and a field "
        "retires through 'active' → 'deprecated' → 'sunset' in separate PRs.\n"
        "To retire a field: mark it 'deprecated' in the widget definition and "
        "regenerate; once that has merged, mark it 'sunset' and regenerate "
        "again with:\n"
        f"  {regen_command(detect_scope(Path.cwd()))}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
