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

from conformance.suite.schema.disposition import RuleScope

_LEDGER_RELPATH = ("data", "contract_schema.lock.json")


def regen_command(scope: RuleScope | None = None) -> str:
    """Return the ledger-regeneration command to prescribe, pinned to *this* version.

    The version pin is the point.  ``detect`` runs from an ephemeral, unpinned
    install (``uvx atlan-application-sdk-conformance`` — see
    ``.github/actions/run-conformance-detect/action.yaml``), while a bare
    ``uv run atlan-application-sdk-conformance`` in a consumer app resolves that
    repo's *locked* dev dependency.  Those are two different versions whenever
    the repo's lock lags the latest release, and the generator's output is
    version-dependent: the SDK contract-base registry it reads
    (``_sdk_contract_mixins``) grows as the SDK gains fields.  A B006 finding
    raised by the newer checker then has a prescribed remedy that the older
    generator cannot satisfy — it rewrites the ledger byte-identically and the
    finding survives, which is how FND-607 sent a developer to a dead end on a
    BLOCK-tier rule.  Pinning to :data:`conformance.__version__` makes the
    remedy reproduce the checker's own field set.

    In the SDK repo (*scope* is :attr:`RuleScope.SDK`) the suite is in-tree and
    ``uv run`` is the only correct invocation — a published-wheel pin there would
    regenerate against whatever was last released, not the working tree.
    """
    from conformance import __version__

    if scope is RuleScope.SDK:
        return "uv run atlan-application-sdk-conformance gen-contract-ledger"
    return f"uvx atlan-application-sdk-conformance=={__version__} gen-contract-ledger"


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


def load_ledger(
    path: Path | None = None, *, repo_root: Path | None = None
) -> ContractLedger:
    """Load the committed ledger.

    Resolution order (first match wins):
    1. *path* — explicit override used by tests and the generator.
    2. ``ATLAN_CONTRACT_LEDGER_PATH`` env var — CI override.
    3. ``<repo_root>/contract_schema.lock.json`` — the app's committed ledger
       when the detector is given a repo root (the normal B005/B006 path for
       consumer apps).  Skipped when the file is absent so the SDK, which
       keeps its ledger inside the package, falls through to step 4.
    4. Package data — the SDK's own bundled ledger (fallback / SDK-self-scan).

    A genuinely **absent** ledger yields an empty result silently (graceful
    degradation — an older wheel without the file simply produces no B005
    findings).  A **malformed** ledger is different and is reported to stderr.
    """
    if path is None:
        env_override = os.environ.get("ATLAN_CONTRACT_LEDGER_PATH")
        if env_override:
            path = Path(env_override)
        elif repo_root is not None:
            candidate = repo_root / "contract_schema.lock.json"
            if candidate.exists():
                path = candidate
    try:
        if path is None:
            text = (
                _ir.files("conformance")
                .joinpath(*_LEDGER_RELPATH)
                .read_text(encoding="utf-8")
            )
        else:
            text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
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
            f"(`{regen_command()}`).",
            file=sys.stderr,
        )
        return ContractLedger(version=LEDGER_VERSION, fields=[])
    return _parse(payload)


def load_ledger_baseline(outfile: Path) -> ContractLedger:
    """The ledger to build a *write* on top of — empty when *outfile* is absent.

    Every writer (``gen-contract-ledger`` and the ``bootstrap`` scaffold) must
    start a first ledger from EMPTY, never from :func:`load_ledger` with no
    path.  That call falls through to resolution step 4, the SDK's own packaged
    ledger, and ``build_ledger`` is append-only — so all six SDK template
    contracts (``QueryExtractionInput``, ``ExtractionInput``, …) get copied into
    the consumer's brand-new ledger and can never be removed again.  Any app
    class sharing one of those names then draws B005 "field removed" for every
    SDK field it does not have, permanently.

    Reading a ledger to *check* against is a different question and stays with
    :func:`load_ledger`, fallback and all.  This helper exists so the
    empty-start invariant has one definition that both writers share.
    """
    return (
        load_ledger(outfile)
        if outfile.exists()
        else ContractLedger(version=LEDGER_VERSION, fields=[])
    )
