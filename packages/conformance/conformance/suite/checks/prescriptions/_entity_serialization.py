"""P052 EntitySerializationBypass — app serializes an asset around ``entity_bytes``.

``application_sdk.common.asset_serialization.entity_bytes`` is the SDK's one
serialization seam for a mapper result (FND-2056 / FND-2137): it owns the
dispatch, the ``connectionName`` injection, the declared entity envelope and the
placeholder-guid strip (FND-2720).  A fix made there reaches every connector
that goes through it — and none that does not.  An app that turns a pyatlan
asset into wire output itself (``asset.to_nested_bytes()``) silently opts out of
every one of those, and because reference apps are copied, the bypass spreads.

Per-file.  Flags, in hand-written app code only (``app/`` minus
``app/generated/``; ``tests/`` never reaches the scan — see ``discover``):

* any ``<x>.to_nested_bytes(...)`` / ``<x>.to_nested_dict(...)`` call.  The
  receiver is not resolved: both names are pyatlan-asset serializers and no
  other type in an app carries them.
* a ``to_atlas_format(...)`` call that resolves to ``pyatlan_v9`` — a bare name
  imported from it (aliased or not), or an attribute call through a module bound
  to it (``transform.to_atlas_format``, ``pyatlan_v9.model.transform.…``).  A
  same-named local helper is left alone.

A call to ``entity_bytes`` itself is never flagged, and neither is anything it
calls internally — the SDK is not in scope.

WARN tier.  A genuine non-entity use — a ``ConnectionRef`` built from
``to_atlas_format``, as the SDK's own ``contracts/types.py`` does — is the one
sanctioned carve-out, suppressed inline with a reason.
"""

from __future__ import annotations

import ast
from pathlib import PurePath

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    collect_import_origins,
    make_finding,
)
from conformance.suite.schema.findings import Finding

#: Asset methods that emit the wire shape directly.
_ASSET_SERIALIZERS: frozenset[str] = frozenset({"to_nested_bytes", "to_nested_dict"})

#: The pyatlan_v9 module-level encoder, matched only when it resolves there.
_ATLAS_FORMAT = "to_atlas_format"
_PYATLAN_V9 = "pyatlan_v9"

_HINT = (
    "Serialize through "
    "`application_sdk.common.asset_serialization.entity_bytes(asset, envelope=...)`"
)


def _in_app_source(filename: str) -> bool:
    """True for hand-written app code: under ``app/`` but not ``app/generated/``."""
    parts = PurePath(filename).parts
    if not parts or parts[0] != "app":
        return False
    return not (len(parts) > 2 and parts[1] == "generated")


def _dotted_name(node: ast.expr) -> str | None:
    """Flatten ``a.b.c`` to ``"a.b.c"``; ``None`` if the chain is not all names."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _is_pyatlan_atlas_format(func: ast.expr, origins: dict[str, str]) -> bool:
    """True when *func* names ``to_atlas_format`` imported from ``pyatlan_v9``."""
    dotted = _dotted_name(func)
    if dotted is None:
        return False
    root, _, rest = dotted.partition(".")
    origin = origins.get(root)
    if origin is None:
        return False
    # Resolve before comparing names: an aliased import (``… import
    # to_atlas_format as encode``) is only recognisable through its origin.
    resolved = f"{origin}.{rest}" if rest else origin
    parts = resolved.split(".")
    return parts[0] == _PYATLAN_V9 and parts[-1] == _ATLAS_FORMAT


def check_p052(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P052 for asset serialization that does not go through ``entity_bytes``."""
    if not _in_app_source(filename):
        return []
    origins = collect_import_origins(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _ASSET_SERIALIZERS:
            what = f"`.{func.attr}()`"
        elif _is_pyatlan_atlas_format(func, origins):
            what = "`pyatlan_v9` `to_atlas_format()`"
        else:
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P052",
                node=node,
                message=(
                    f"asset serialized with {what}, bypassing the SDK's "
                    "serialization seam — connectionName injection, the entity "
                    "envelope and placeholder-guid stripping never apply. "
                    f"{_HINT}."
                ),
                directives=directives,
            )
        )
    return findings
