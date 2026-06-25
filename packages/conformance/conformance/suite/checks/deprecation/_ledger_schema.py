"""Contract ledger schema — committed data for B005/B006 fleet-wide enforcement.

The committed ledger (``contract_schema.lock.json``) is the append-only baseline
that B005 uses to detect non-additive contract changes (field removal, type
change).  B006 fires when a live field is not in the ledger (stale).

This module is the single definition of the ledger's shape, its on-disk
location, and how it is built from contract source — shared by the generator
(``conformance.tools.generate_contract_ledger``) and the B005/B006 checker so
the producer and reader can never disagree about the format.
"""

from __future__ import annotations

import importlib.resources as _ir
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

_LEDGER_RELPATH = ("data", "contract_schema.lock.json")


def _ledger_path() -> Path:
    """Resolve the committed ledger as a filesystem path for writing."""
    return Path(str(_ir.files("conformance"))).joinpath(*_LEDGER_RELPATH)


LEDGER_PATH = _ledger_path()
LEDGER_VERSION = 1


@dataclass(frozen=True)
class ContractField:
    """One field entry in the contract ledger."""

    contract: str
    field: str
    type: str  # canonical normalized annotation string, frozen on first record
    status: str  # "active" | "deprecated" | "sunset"


@dataclass
class ContractLedger:
    """The full contract schema ledger."""

    version: int
    fields: list[ContractField]


def serialize(ledger: ContractLedger) -> str:
    """Render *ledger* to canonical JSON (sorted by contract+field, trailing newline)."""
    payload = {
        "version": ledger.version,
        "fields": sorted(
            [asdict(f) for f in ledger.fields],
            key=lambda r: (r["contract"], r["field"]),
        ),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _parse(payload: dict) -> ContractLedger:
    fields = [
        ContractField(
            contract=r["contract"],
            field=r["field"],
            type=r["type"],
            status=r.get("status", "active"),
        )
        for r in payload.get("fields", [])
    ]
    return ContractLedger(version=payload.get("version", LEDGER_VERSION), fields=fields)


def load_ledger(path: Path | None = None) -> ContractLedger:
    """Load the committed ledger.

    With *path* ``None`` (the normal B005/B006 path) reads from the
    ``conformance`` package resource.  A *path* override is used by tests.

    A genuinely **absent** ledger yields an empty result silently (graceful
    degradation — an older wheel without the file simply produces no B005
    findings).  A **malformed** ledger is different and is reported to stderr.
    """
    if path is None:
        env_override = os.environ.get("ATLAN_CONTRACT_LEDGER_PATH")
        path = Path(env_override) if env_override else None
    try:
        if path is None:
            text = (
                _ir.files("conformance")
                .joinpath(*_LEDGER_RELPATH)
                .read_text(encoding="utf-8")
            )
        else:
            text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ContractLedger(version=LEDGER_VERSION, fields=[])
    except OSError as exc:  # pragma: no cover
        print(f"warning: could not read contract ledger: {exc}", file=sys.stderr)
        return ContractLedger(version=LEDGER_VERSION, fields=[])
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        print(
            f"warning: contract ledger is malformed JSON ({exc}); "
            "B005/B006 contract-compat checks are disabled until it is regenerated "
            "(`atlan-application-sdk-conformance gen-contract-ledger`).",
            file=sys.stderr,
        )
        return ContractLedger(version=LEDGER_VERSION, fields=[])
    return _parse(payload)
