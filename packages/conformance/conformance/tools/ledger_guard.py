"""Append-only guard for the contract schema ledger.

Validates that no entry was deleted from ``contract_schema.lock.json``, no
recorded ``type`` was changed, no entry moved straight from ``active`` to
``sunset``, no entry moved from not required to required, and no required entry
was added to a contract the base ledger already records, between the base ref
and HEAD.  Additions and every other status change are allowed.  Retirement
runs ``active`` → ``deprecated`` → ``sunset`` across separate merges, so callers
always see a deprecation before a field is withdrawn; and a caller that predates
a field never has to send it.  An unknown ``required`` in the base (a ledger
written before the key existed) may be backfilled with either value.

Exit codes
----------
0  All checks pass (no deletions, no type changes, no skipped deprecation, no
   newly required field).
1  A deletion, type change, ``active`` → ``sunset`` move or newly required
   field was detected — block the PR.
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


def _index_required(payload: dict) -> dict[tuple[str, str], object]:
    """Index ledger requiredness as {(contract, field): required}.

    The raw value: ``True``/``False``, ``None`` when absent (unknown), or
    anything else a hand edit wrote, which :func:`check` reports.
    """
    return {
        (entry.get("contract", ""), entry.get("field", "")): entry.get("required")
        for entry in payload.get("fields", [])
    }


def check(
    base_payload: dict | None,
    head_payload: dict | None,
) -> tuple[bool, list[str]]:
    """Compare base and head ledger payloads.

    Returns (passed, error_messages).  ``passed`` is True when no deletions,
    type changes, ``active`` → ``sunset`` moves, newly required fields or
    unknown HEAD statuses or requiredness are detected.  An absent
    *base_payload* (no prior ledger) has no entries to compare against, so
    only HEAD's own values are checked.
    """
    if base_payload is None:
        errors = _invalid_values(head_payload) if head_payload is not None else []
        return len(errors) == 0, errors

    errors: list[str] = []

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
    base_required = _index_required(base_payload)
    head_required = _index_required(head_payload)
    base_contracts = {contract for contract, _ in base_index}

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
            if (
                base_required[(contract, field)] is False
                and head_required[(contract, field)] is True
            ):
                errors.append(
                    f"NEWLY REQUIRED: {contract}.{field} not required → required "
                    "— callers that omit it fail validation. Keep a default."
                )

    for (contract, field), required in head_required.items():
        if (
            required is True
            and (contract, field) not in base_index
            and contract in base_contracts
        ):
            errors.append(
                f"NEW REQUIRED FIELD: {contract}.{field} — a field added to an "
                "existing contract must have a default; callers that predate it "
                "do not send it. Give it a default and regenerate."
            )

    errors.extend(_invalid_values(head_payload))
    return len(errors) == 0, errors


def _invalid_values(payload: dict) -> list[str]:
    """Report statuses and requiredness no regeneration writes."""
    errors: list[str] = []
    for (contract, field), required in _index_required(payload).items():
        if required is not None and not isinstance(required, bool):
            errors.append(
                f"INVALID REQUIRED: {contract}.{field} {required!r} — a ledger "
                "'required' is true, false or absent; regenerate instead of "
                "editing it by hand."
            )
    for (contract, field), status in _index_statuses(payload).items():
        if status not in _STATUSES:
            errors.append(
                f"INVALID STATUS: {contract}.{field} {status!r} — a ledger "
                "status is one of 'active', 'deprecated' or 'sunset'; "
                "regenerate instead of editing it by hand."
            )
    return errors


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
        "Field deletions and type changes are not permitted, a field "
        "retires through 'active' → 'deprecated' → 'sunset' in separate PRs, "
        "and a field callers must send cannot be added to an existing contract "
        "or made required later.\n"
        "To retire a field: mark it 'deprecated' in the widget definition and "
        "regenerate; once that has merged, mark it 'sunset' and regenerate "
        "again with:\n"
        f"  {regen_command(detect_scope(Path.cwd()))}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
