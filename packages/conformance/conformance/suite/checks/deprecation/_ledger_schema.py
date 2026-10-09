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

_SDK_LEDGER_NAME = "contract_schema.lock.json"
_LEDGER_RELPATH = ("data", _SDK_LEDGER_NAME)


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
    """Where an installed conformance package carries the SDK's ledger.

    Read-only package data: the wheel's build hook copies it from the SDK
    repository's root ``contract_schema.lock.json``, which is the only committed
    SDK ledger and the only one the SDK writes (FND-3108). In a source tree this
    path does not exist.
    """
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
            # An explicit null reads as active, like an absent key: left as
            # None it would match neither 'active' nor 'sunset' in B005.
            status=r.get("status") or "active",
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
    3. ``<repo_root>/contract_schema.lock.json`` — the repo's committed
       ledger when the detector is given a repo root (the normal B005/B006
       path for consumer apps and for the SDK's own self-scan).

    With none of these, the ledger is empty. The SDK's packaged ledger is never
    a stand-in for a repo's own: it describes the SDK's contracts, not the
    repo's, and :func:`load_sdk_ledger` is the one way to read it.

    A genuinely **absent** ledger yields an empty result silently (a repo that
    has not generated one yet produces no B005 findings).  A **malformed**
    ledger is different and is reported to stderr.
    """
    if path is None:
        env_override = os.environ.get("ATLAN_CONTRACT_LEDGER_PATH")
        if env_override:
            path = Path(env_override)
        elif repo_root is not None:
            candidate = repo_root / "contract_schema.lock.json"
            if candidate.exists():
                path = candidate
    if path is None:
        return ContractLedger(version=LEDGER_VERSION, fields=[])
    try:
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


def _source_tree_sdk_ledger() -> Path | None:
    """The SDK repo's root ledger when this package runs from an SDK checkout."""
    package_dir = Path(str(_ir.files("conformance")))
    sdk_root = package_dir.parent.parent.parent
    candidate = sdk_root / _SDK_LEDGER_NAME
    if candidate.is_file() and (sdk_root / "application_sdk").is_dir():
        return candidate
    return None


def load_sdk_ledger() -> ContractLedger:
    """The SDK's own ledger bundled in this package, ignoring every override.

    B005 reads it in a consumer app to tell an SDK-retired template field from
    an app-made removal: the SDK records a deliberate retirement as 'sunset'
    here, and this copy ships in the same release whose template registry no
    longer lists the field. Unlike :func:`load_ledger`, neither the env override
    nor the app's own ledger may stand in for it.

    Run from an SDK source tree (an editable install, ``PYTHONPATH``), the
    root ``contract_schema.lock.json`` is read, so a stale untracked packaged
    copy left in the checkout never stands in for it.  An installed package
    reads the copy its build hook packaged from that root file.  A package with
    neither
    is a broken build: the exemption is then disabled, and stderr says so
    rather than letting every SDK-retired field resurface as B005 unexplained.
    """
    packaged = _ir.files("conformance").joinpath(*_LEDGER_RELPATH)
    source = _source_tree_sdk_ledger() or (packaged if packaged.is_file() else None)
    if source is None:
        print(
            "warning: this conformance package carries no SDK contract ledger "
            f"({'/'.join(_LEDGER_RELPATH)}); B005 cannot recognise SDK-retired "
            "contract fields until it is reinstalled from a complete build.",
            file=sys.stderr,
        )
        return ContractLedger(version=LEDGER_VERSION, fields=[])
    try:
        return _parse(json.loads(source.read_text(encoding="utf-8")))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        print(
            f"warning: the SDK contract ledger is unreadable ({exc!r}); B005 "
            "cannot recognise SDK-retired contract fields.",
            file=sys.stderr,
        )
        return ContractLedger(version=LEDGER_VERSION, fields=[])


def load_ledger_baseline(outfile: Path) -> ContractLedger:
    """The ledger to build a *write* on top of — empty when *outfile* is absent.

    Every writer (``gen-contract-ledger`` and the ``bootstrap`` scaffold) must
    start a first ledger from EMPTY, never from anything else on hand.  An
    earlier :func:`load_ledger` fell back to the SDK's own packaged ledger, and
    ``build_ledger`` is append-only — so all six SDK template contracts
    (``QueryExtractionInput``, ``ExtractionInput``, …) got copied into a
    consumer's brand-new ledger and could never be removed again.  Any app class
    sharing one of those names then drew B005 "field removed" for every SDK
    field it did not have, permanently.  That fallback is gone (FND-3108), but
    the invariant stays explicit here.

    This helper exists so the empty-start invariant has one definition that
    both writers share.
    """
    return (
        load_ledger(outfile)
        if outfile.exists()
        else ContractLedger(version=LEDGER_VERSION, fields=[])
    )
