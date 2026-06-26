"""P020 LegacyPyatlanAssetImport — flag non-v9 ``pyatlan.model.assets`` imports.

New connectors must build assets from ``pyatlan_v9.model.assets`` (the optimized
v9 surface the asset-mapper pattern is built on, BLDX-1492).  The legacy
``pyatlan.model.assets`` classes are the transformer-era serialization path,
retained only for connectors still on the built-in ``AtlasTransformer``.

Detection is import-anchored (the import is the unambiguous signal — asset model
classes are only imported to construct assets) and scoped *strictly* to
``pyatlan.model.assets``: the rest of ``pyatlan`` (notably
``pyatlan.model.enums``, which has no v9 equivalent) is never matched.  Three
import forms are recognised:

* ``from pyatlan.model.assets import Table``        (from-import of the package)
* ``import pyatlan.model.assets``                    (module import)
* ``from pyatlan.model import assets``               (submodule from-import)
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_ASSETS_MODULE = "pyatlan.model.assets"
_MESSAGE = (
    "Imports legacy 'pyatlan.model.assets' — build assets from "
    "'pyatlan_v9.model.assets' instead (the optimized v9 surface the asset-mapper "
    "pattern uses; ships inside the existing pyatlan>=9 dependency). Not a drop-in "
    "rename: v9 attribute names and serialization (asset.to_nested_bytes()) differ."
)


def _is_legacy_assets_module(module: str | None) -> bool:
    """True if *module* is the legacy assets package (and not the v9 namespace)."""
    return module is not None and (
        module == _ASSETS_MODULE or module.startswith(f"{_ASSETS_MODULE}.")
    )


def check_p020(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P020 for any import of legacy ``pyatlan.model.assets``."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from pyatlan.model.assets import X`
            hit = _is_legacy_assets_module(node.module)
            # `from pyatlan.model import assets`
            if not hit and node.module == "pyatlan.model":
                hit = any(alias.name == "assets" for alias in node.names)
            if hit:
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P020",
                        node=node,
                        message=_MESSAGE,
                        directives=directives,
                    )
                )
        elif isinstance(node, ast.Import):
            # `import pyatlan.model.assets [as x]`
            for alias in node.names:
                if _is_legacy_assets_module(alias.name):
                    findings.append(
                        make_finding(
                            filename=filename,
                            rule_id="P020",
                            node=node,
                            message=_MESSAGE,
                            directives=directives,
                        )
                    )
                    break
    return findings
