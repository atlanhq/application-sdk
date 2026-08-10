"""O006 DirectRocksdictImport — flag app code importing ``rocksdict`` directly.

The SDK ships ``application_sdk.common.spillable_dict.SpillableDict`` — a
``MutableMapping``-compatible, disk-backed dict built on ``rocksdict.Rdict``
that pickles values directly (no hand-rolled serialization step). It exists
specifically so connector apps stop hand-rolling their own RocksDB wrapper.

Detection is import-anchored (a direct ``rocksdict`` import is the unambiguous
signal — nothing else pulls that dependency in) and, symmetrically with O004,
scoped to app code only: ``application_sdk.common.spillable_dict`` and
``application_sdk.common.incremental.storage.rocksdb_utils`` are themselves
the intended callers of ``rocksdict`` and are not subject to this rule (see
``RuleScope.APP`` on the catalog entry — the SDK is the publisher, not a
consumer, of this seam).

Motivating incident (CNCT-80/CNCT-191, 2026-08): ``atlan-thoughtspot-app`` and
``atlan-aws-smus-app`` each independently hand-rolled a ``DiskLookup`` class
directly on ``rocksdict.Rdict`` with a hand-rolled JSON serialize/deserialize
step — ``put()`` special-cased ``str`` (stored raw), ``get()`` unconditionally
ran ``json.loads()`` on every read. A stored string that happened to also be
valid bare JSON (a numeric-looking name, ``"true"``, ``"null"``) silently came
back as ``int``/``bool``/``None`` instead of ``str``, corrupting output columns
and crashing the parquet writer downstream. Two connectors independently wrote
the same bug because there was no fleet-wide signal nudging either one toward
the SDK's existing, already-correct ``SpillableDict``.

Three import forms are recognised, mirroring O004's shape:

* ``from rocksdict import Rdict``        (from-import of the package)
* ``import rocksdict``                   (module import)
* ``from rocksdict import Rdict as X``   (aliased from-import)
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_ROCKSDICT_MODULE = "rocksdict"
_MESSAGE = (
    "Imports 'rocksdict' directly — prefer "
    "'application_sdk.common.spillable_dict.SpillableDict', the SDK's "
    "MutableMapping-compatible disk-backed dict built on the same library. "
    "Not a drop-in import swap: SpillableDict pickles values (no hand-rolled "
    "JSON serialize/deserialize step to get wrong) and restricts keys to "
    "str/int/float/bool/bytes — review call sites before migrating."
)


def _is_rocksdict_module(module: str | None) -> bool:
    """True if *module* is the ``rocksdict`` package (or a submodule of it)."""
    return module is not None and (
        module == _ROCKSDICT_MODULE or module.startswith(f"{_ROCKSDICT_MODULE}.")
    )


def check_o006(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit O006 for any direct import of ``rocksdict`` in app code."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from rocksdict import Rdict[, Options, ...]`
            if _is_rocksdict_module(node.module):
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="O006",
                        node=node,
                        message=_MESSAGE,
                        directives=directives,
                    )
                )
        elif isinstance(node, ast.Import):
            # `import rocksdict [as x]`
            for alias in node.names:
                if _is_rocksdict_module(alias.name):
                    findings.append(
                        make_finding(
                            filename=filename,
                            rule_id="O006",
                            node=node,
                            message=_MESSAGE,
                            directives=directives,
                        )
                    )
                    break
    return findings
