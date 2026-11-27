"""Field registries for SDK-provided contract classes.

``resolve_contract_fields`` in the neutral ``_entrypoint_contract_fields``
module resolves contract fields across the full inheritance hierarchy — used
by the B005/B006 checker, K016/K018, preflight and the ledger generator.
In-repo base classes are resolved directly from source via the cross-file class
registry (``collect_classes`` / ``by_name``). SDK-provided contract classes are
*not* part of the scanned repo when a checker runs against a consumer app, so
their fields come from a table baked into the wheel instead:
``conformance/data/sdk_contract_fields.json``, generated from SDK source by

    uv run atlan-application-sdk-conformance gen-sdk-contract-fields

and loaded here into four registries:

- :data:`SDK_CONTRACT_BASE_FIELDS` — the bases and mixins in
  ``application_sdk.contracts.base`` (``Input`` / ``Output`` /
  ``PublishInputMixin``), each carrying only its own declared fields.
- :data:`SDK_TEMPLATE_CONTRACT_FIELDS` — the template contracts in
  ``application_sdk.templates.contracts``, each carrying its fields *flattened*
  across its own base chain.
- :data:`SDK_TEMPLATE_CONTRACT_BASES` — each template contract's registered
  ancestors.
- :data:`SDK_TEMPLATE_RUN_CONTRACTS` — the Input/Output type names of each SDK
  template's ``run()``.

Do not hand-edit the JSON. ``tests/test_sdk_contract_fields_drift.py`` runs the
generator with ``--check`` and fails until the table is regenerated in the same
PR as the SDK change. See ``conformance/tools/generate_sdk_contract_fields.py``
for how each table is derived.

**Template contracts — scope.** Every contract class declared under
``application_sdk/templates/contracts/`` — a mechanical boundary rather than a
curated shortlist, because ``resolve_contract_fields`` returns no fields for
*any* template name it does not find here, so a partial catalog just defers the
same false positive to the next class a consumer subclasses.

**Template contracts — flattened.** Each entry carries the class's own fields
plus every field it inherits, including from other template contracts.
Flattening is required, not an optimisation — the lookup is by the single
unresolved base name and does not recurse, so ``IncrementalExtractionOutput``
cannot reach ``ExtractionOutput``'s fields through its own registry entry.

**Template contracts — excluded.** Bare names that another SDK contract module
also declares. The registry is keyed by bare class name, matching how the
resolver sees an unresolved base, so it cannot tell two such classes apart;
registering either one's fields would mis-resolve consumers of the other.
Leaving them out keeps the conservative behaviour — no fields resolved, as for
every unregistered name. The set is empty: the one collision,
``UploadInput`` / ``UploadOutput`` in both
``templates.contracts.base_metadata_extraction`` and ``contracts.storage``, was
renamed to ``UploadToAtlanInput`` / ``UploadToAtlanOutput`` on the template side
(FND-4095). The old template names survive only as deprecated aliases, which
declare no class, so a consumer subclassing them still resolves no fields.
``tests/test_sdk_contract_mixins.py`` pins that the set stays empty.

**Template run() contracts.** An app that subclasses a template and writes no
``run()`` of its own inherits the template's, and ``_collect_implicit_ep``
registers it with the input type ``get_type_hints`` reports on that inherited
method. The runtime validates the payload against exactly that model, so K018
pairs such an app with it. ``BaseMetadataExtractor`` is absent on purpose: its
``run`` is still ``App.run``, so a subclass without its own ``run()`` has no
implicit entrypoint at all.

**Missing, unreadable or empty table.** Import fails. Empty registries would not
be a safe fallback: every consumer contract inheriting an SDK base would resolve
to no inherited fields, which is a fleet-wide B005/B006/K018 false positive. The
one exception is the generator itself: it sets :data:`BOOTSTRAP_ENV` around its
import so it can recreate a missing or broken table. It never reads these
registries, because every SDK class it resolves is found in SDK source.
"""

from __future__ import annotations

import importlib.resources as _ir
import os
from pathlib import Path
from typing import NamedTuple, TypedDict, cast

from conformance.suite.checks._ast_common._io import safe_read_json

DATA_PATH = Path(str(_ir.files("conformance"))).joinpath(
    "data", "sdk_contract_fields.json"
)

#: Set to ``"1"`` only by ``generate_sdk_contract_fields`` while it imports the
#: resolver, so a missing or broken table can be regenerated. Never set it
#: anywhere else: with it set, a broken table loads as empty registries.
BOOTSTRAP_ENV = "_CONFORMANCE_SDK_CONTRACT_FIELDS_BOOTSTRAP"


class SdkField(NamedTuple):
    """One recorded field on an SDK contract class."""

    name: str
    canonical_type: str
    status: str  # "active" | "deprecated" | "sunset"


class TemplateRunContract(NamedTuple):
    """The Input and Output type names of an SDK template's ``run()``."""

    input: str
    output: str


def _fields(raw: dict[str, list[list[str]]]) -> dict[str, tuple[SdkField, ...]]:
    return {
        name: tuple(SdkField(*entry) for entry in entries)
        for name, entries in raw.items()
    }


class _Table(TypedDict):
    """Shape of ``sdk_contract_fields.json`` as the generator writes it."""

    contract_base_fields: dict[str, list[list[str]]]
    template_contract_fields: dict[str, list[list[str]]]
    template_contract_bases: dict[str, list[str]]
    template_run_contracts: dict[str, dict[str, str]]


class SdkContractTableError(RuntimeError):
    """The packaged SDK contract-field table is missing, unreadable or empty."""


_SECTIONS: tuple[str, ...] = (
    "contract_base_fields",
    "template_contract_fields",
    "template_contract_bases",
    "template_run_contracts",
)
_CONTRACT_ROOTS: frozenset[str] = frozenset({"Input", "Output", "PublishInputMixin"})


def _validate_table(raw: object) -> str | None:
    """Why *raw* is not a usable table, or ``None`` when it is."""
    if not isinstance(raw, dict):
        return "missing or not valid JSON"
    for section in _SECTIONS:
        value = raw.get(section)
        if not isinstance(value, dict) or not value:
            return f"section {section!r} is missing or empty"
    missing_roots = _CONTRACT_ROOTS - set(raw["contract_base_fields"])
    if missing_roots:
        return f"contract_base_fields lacks {sorted(missing_roots)}"
    return None


def _empty_table() -> _Table:
    return {
        "contract_base_fields": {},
        "template_contract_fields": {},
        "template_contract_bases": {},
        "template_run_contracts": {},
    }


def load_table(path: Path = DATA_PATH) -> _Table:
    """Read and validate the table at *path*.

    Raises:
        SdkContractTableError: the table is missing, unreadable, or lacks a
            section, unless :data:`BOOTSTRAP_ENV` is set, in which case an empty
            table is returned so the generator can rebuild it.
    """
    raw = safe_read_json(path)
    problem = _validate_table(raw)
    if problem is None:
        return cast(_Table, raw)
    if os.environ.get(BOOTSTRAP_ENV) == "1":
        return _empty_table()
    raise SdkContractTableError(
        f"{path}: {problem} — regenerate it with "
        "`uv run atlan-application-sdk-conformance gen-sdk-contract-fields`."
    )


_TABLE = load_table()

SDK_CONTRACT_BASE_FIELDS: dict[str, tuple[SdkField, ...]] = _fields(
    _TABLE["contract_base_fields"]
)

SDK_TEMPLATE_CONTRACT_FIELDS: dict[str, tuple[SdkField, ...]] = _fields(
    _TABLE["template_contract_fields"]
)

SDK_TEMPLATE_CONTRACT_BASES: dict[str, frozenset[str]] = {
    name: frozenset(bases) for name, bases in _TABLE["template_contract_bases"].items()
}

SDK_TEMPLATE_RUN_CONTRACTS: dict[str, TemplateRunContract] = {
    name: TemplateRunContract(entry["input"], entry["output"])
    for name, entry in _TABLE["template_run_contracts"].items()
}

del _TABLE

# ── Model-declared artifact fields ────────────────────────────────────────────
# A ``FileReference`` field can carry the SDK's ``AssetArtifact`` marker
# (``application_sdk.contracts.types``), which says its declaration *is* an
# executable model — ``pyatlan_v9``'s ``Asset`` — rather than a hand-authored
# field map in ``artifactSchemas``. The SDK's registration-time guard exempts
# such a field, and its activity interceptor validates it against the whole
# model instead, so K016 must exempt it too: a rule demanding a declaration the
# SDK does not want, for an artifact it already checks more strictly, would be
# asking every app to author a partial restatement of ``Asset`` (FND-1863).
#
# Names, not a per-class map, and that is a deliberate accuracy trade. The
# marker lives in ``Annotated`` metadata, which ``_canonical_type`` strips, so a
# field *inherited* from an SDK contract arrives at the check as a bare name with
# no annotation left to inspect — the same reason ``SDK_TEMPLATE_CONTRACT_FIELDS``
# exists at all. A field an app declares itself is matched on its own annotation
# instead (see ``artifact_schema_declared._check``), so the only case this set
# answers is the inherited one, where the app has nothing of its own to mark.
#
# The residual false negative is an app declaring an unrelated boundary field
# that happens to share one of these names *and* inheriting it from a base this
# scan cannot see. K016 is a WARN-tier rule that errs toward a false negative by
# design, and this gap is narrower than the import-alias one it already accepts.
#
# ``tests/test_sdk_contract_mixins.py`` rebuilds this set from an AST scan of the
# installed SDK's contract modules, so a newly marked field cannot ship without
# the rule learning about it, and a name the SDK has stopped marking cannot sit
# here exempting fields forever — see the ahead-of-pin allowlist below for the one
# window in which an entry may have no live marker backing it.
SDK_MODEL_BACKED_ARTIFACT_FIELDS: frozenset[str] = frozenset({"transformed_files"})

#: Entries of :data:`SDK_MODEL_BACKED_ARTIFACT_FIELDS` the *pinned* SDK does not
#: mark yet, because the marker landed in the SDK after this package's pin.
#:
#: The two drift directions need different rules, and this is what lets both be
#: checked. A name the SDK marks and the mirror omits is a fleet-wide false
#: positive, so that direction is absolute. A mirror entry with no live marker is
#: either (a) this window — the marker is on the SDK's ``main`` and the pin has
#: not caught up — or (b) genuine staleness, where a removed or renamed marker
#: leaves a name behind that goes on exempting every inherited field sharing it,
#: silently and with the test still green. Only (a) is legitimate, and only (a) is
#: listed here.
#:
#: **Every entry is a deletion waiting for a pin bump.** Once
#: ``packages/conformance/uv.lock`` moves to an SDK that carries the marker, the
#: drift test sees it live and fails until the name is removed from this
#: allowlist — which is the point: the exemption stays, the temporary excuse for
#: it does not.
MODEL_BACKED_FIELDS_AHEAD_OF_PIN: frozenset[str] = frozenset()
